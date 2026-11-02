"""XMP sidecars (WU-3.3).

This is the unit that makes D-1 true rather than aspirational, so the
acceptance criteria are unusually literal and one of them is unusually
important: **a sidecar must be readable by third-party software**. A test that
reads back what our own writer produced, with our own reader, proves that the
module agrees with itself — which is not the claim. So the round-trip tests are
here for correctness, and separately every property is read back with
**exiv2**, which is a different implementation by different people and the one
digiKam is built on.

The other thing this file leans on is that a sidecar must not *lose* anything.
Tags surviving this app's death is the entire point; an app that wipes
Lightroom's keywords on its way past has inverted its own purpose.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from photo_manager.catalog import Catalog
from photo_manager.models import Location, Person, Photo, Session, Tag
from photo_manager.sidecars import (
    Sidecar,
    SidecarError,
    parse_xmp,
    read_sidecar,
    render_xmp,
    sidecar_for,
    sidecar_path,
    write_sidecar,
)

FULL = Sidecar(
    rating=4,
    location="St Aldo",
    people=("Alice", "Carol"),
    event="Beach day",
    tags=("seaside", "golden hour"),
)


@pytest.fixture
def photo(tmp_path: Path) -> Path:
    target = tmp_path / "2026-09-06_st-aldo_DSCF1024.jpg"
    target.write_bytes(b"\xff\xd8 pretend photograph")
    return target


# ----------------------------------------------------------------------
# Where it goes
# ----------------------------------------------------------------------


def test_the_sidecar_keeps_the_photograph_s_whole_filename() -> None:
    assert sidecar_path("a/2026-09-06_DSCF1024.jpg").name == "2026-09-06_DSCF1024.jpg.xmp"


def test_a_jpeg_and_its_raf_get_a_sidecar_each(tmp_path: Path) -> None:
    """§5.2's reason for keeping the extension. Replacing it would give both
    halves of a pair one sidecar, and one of them would silently win."""
    jpeg = sidecar_path(tmp_path / "2026-07-13_DSCF0807.jpg")
    raf = sidecar_path(tmp_path / "2026-07-13_DSCF0807.raf")

    assert jpeg != raf
    assert {jpeg.name, raf.name} == {
        "2026-07-13_DSCF0807.jpg.xmp", "2026-07-13_DSCF0807.raf.xmp",
    }


def test_asking_for_the_sidecar_of_a_sidecar_is_not_a_second_extension() -> None:
    assert sidecar_path("a/x.jpg.xmp").name == "x.jpg.xmp"


# ----------------------------------------------------------------------
# Round trip
# ----------------------------------------------------------------------


def test_every_field_survives_a_round_trip(photo: Path) -> None:
    write_sidecar(photo, FULL)
    assert read_sidecar(photo) == FULL


def test_coordinates_survive_a_round_trip(photo: Path) -> None:
    """Reserved for D-5's map, but a field that silently does not persist is
    worse than one that is not there."""
    located = Sidecar(latitude=50.20576, longitude=-5.48539)
    write_sidecar(photo, located)

    back = read_sidecar(photo)
    assert back is not None
    assert back.latitude == pytest.approx(50.20576, abs=1e-5)
    assert back.longitude == pytest.approx(-5.48539, abs=1e-5)


def test_the_southern_and_western_hemispheres_come_back_negative() -> None:
    """XMP writes a hemisphere letter, not a sign. Getting this backwards puts
    Sydney in the North Pacific."""
    rendered = render_xmp(Sidecar(latitude=-33.8688, longitude=151.2093))
    assert "33,52.1280S" in rendered
    assert "151,12.5580E" in rendered
    assert parse_xmp(rendered).latitude == pytest.approx(-33.8688, abs=1e-5)


def test_an_empty_sidecar_still_records_the_rating(photo: Path) -> None:
    """0 means unrated, which is an answer. The other fields being absent means
    "this app has nothing to say", which is not the same as "it is blank"."""
    write_sidecar(photo, Sidecar())
    text = sidecar_path(photo).read_text(encoding="utf-8")

    assert "<xmp:Rating>0</xmp:Rating>" in text
    assert "Iptc4xmpCore:Location" not in text
    assert "dc:subject" not in text


def test_a_rating_outside_zero_to_five_is_refused() -> None:
    with pytest.raises(ValueError, match="rating"):
        Sidecar(rating=9)


def test_reading_a_photograph_with_no_sidecar_is_not_an_error(photo: Path) -> None:
    """Most photographs will not have one until they are tagged."""
    assert read_sidecar(photo) is None


def test_rewriting_replaces_rather_than_accumulates(photo: Path) -> None:
    write_sidecar(photo, FULL)
    write_sidecar(photo, Sidecar(rating=1, tags=("indoors",)))

    back = read_sidecar(photo)
    assert back == Sidecar(rating=1, tags=("indoors",))
    assert sidecar_path(photo).read_text(encoding="utf-8").count("<xmp:Rating>") == 1


# ----------------------------------------------------------------------
# Not losing other people's work
# ----------------------------------------------------------------------


OTHER_APPS_SIDECAR = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    crs:Exposure2012="+0.65"
    xmp:CreatorTool="Adobe Lightroom">
   <crs:ToneCurveName>Strong Contrast</crs:ToneCurveName>
   <xmp:Rating>2</xmp:Rating>
   <dc:subject><rdf:Bag><rdf:li>lightroom-keyword</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def test_another_application_s_properties_survive_our_write(photo: Path) -> None:
    """The failure D-1 exists to prevent, committed by the app that promised
    to prevent it. Rewriting from scratch would delete the develop settings."""
    sidecar_path(photo).write_text(OTHER_APPS_SIDECAR, encoding="utf-8")

    write_sidecar(photo, Sidecar(rating=5, tags=("ours",)))
    text = sidecar_path(photo).read_text(encoding="utf-8")

    assert "Strong Contrast" in text          # a foreign element
    assert '+0.65' in text                    # a foreign attribute
    assert 'Adobe Lightroom' in text          # a foreign attribute in a shared namespace


def test_our_properties_are_replaced_even_when_they_arrived_as_attributes(
    photo: Path,
) -> None:
    """RDF allows a property as an element or an attribute. Only handling the
    first form would leave two ratings in one file and let a reader pick."""
    sidecar_path(photo).write_text(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
        'xmp:Rating="2"/>'
        '</rdf:RDF></x:xmpmeta>',
        encoding="utf-8",
    )

    write_sidecar(photo, Sidecar(rating=5))

    text = sidecar_path(photo).read_text(encoding="utf-8")
    assert 'xmp:Rating="2"' not in text
    assert read_sidecar(photo) == Sidecar(rating=5)


def test_a_sidecar_that_is_not_valid_xml_is_not_overwritten_blindly(
    photo: Path,
) -> None:
    """Something is in that file. Replacing it with ours, unread, is how a
    corrupt-looking file becomes a definitely-lost one."""
    sidecar_path(photo).write_text("<x:xmpmeta> unclosed", encoding="utf-8")

    with pytest.raises(SidecarError, match="not valid XML"):
        write_sidecar(photo, Sidecar(rating=3))

    assert "unclosed" in sidecar_path(photo).read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# Reading what other people write
# ----------------------------------------------------------------------


def test_a_sequence_is_read_as_well_as_a_bag() -> None:
    """dc:subject is specified as a Bag and plenty of software writes a Seq.
    Refusing to read one would lose exactly the tags D-1 promises to keep."""
    sidecar = parse_xmp(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:subject><rdf:Seq><rdf:li>one</rdf:li><rdf:li>two</rdf:li></rdf:Seq></dc:subject>'
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    )
    assert sidecar.tags == ("one", "two")


def test_properties_spread_over_several_descriptions_are_all_found() -> None:
    """Splitting one namespace per rdf:Description is a common way to write it."""
    sidecar = parse_xmp(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        '<xmp:Rating>3</xmp:Rating></rdf:Description>'
        '<rdf:Description rdf:about="" '
        'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">'
        '<photoshop:City>Falmouth</photoshop:City></rdf:Description>'
        '</rdf:RDF></x:xmpmeta>'
    )
    assert sidecar.rating == 3
    assert sidecar.location == "Falmouth"


def test_a_prefix_this_app_does_not_use_is_still_understood() -> None:
    """A namespace is its URI. Matching on the prefix would mean a file written
    with `ps:` instead of `photoshop:` read as empty."""
    sidecar = parse_xmp(
        '<wrapper xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:zz="http://ns.adobe.com/xap/1.0/">'
        '<zz:Rating>5</zz:Rating></rdf:Description></wrapper>'
    )
    assert sidecar.rating == 5


def test_a_rating_from_elsewhere_is_clamped_rather_than_refused() -> None:
    """XMP's own -1 means rejected. That is someone else's vocabulary, not a
    reason to refuse to read the rest of their file."""
    assert parse_xmp(
        '<wrapper xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        '<xmp:Rating>-1</xmp:Rating></rdf:Description></wrapper>'
    ).rating == 0


def test_unreadable_xml_raises_rather_than_reading_as_empty(photo: Path) -> None:
    """Returning an empty Sidecar would look exactly like "no tags", and the
    next write would make that true."""
    sidecar_path(photo).write_text("not xml at all <<<", encoding="utf-8")
    with pytest.raises(SidecarError):
        read_sidecar(photo)


# ----------------------------------------------------------------------
# FR-5.6 — a failed write is never swallowed
# ----------------------------------------------------------------------


def test_a_write_that_cannot_happen_raises(tmp_path: Path) -> None:
    with pytest.raises(SidecarError, match="could not be written"):
        write_sidecar(tmp_path / "no" / "such" / "folder" / "x.jpg", Sidecar(rating=1))


def test_a_failed_write_leaves_no_temporary_behind(tmp_path: Path) -> None:
    """A half-written sidecar that parses to something almost right is worse
    than none at all."""
    photo = tmp_path / "x.jpg"
    photo.write_bytes(b"x")
    sidecar_path(photo).mkdir()  # a directory exactly where the file must go

    with pytest.raises(SidecarError):
        write_sidecar(photo, Sidecar(rating=1))

    assert list(tmp_path.glob("*.tmp")) == []


def test_the_write_is_atomic(photo: Path) -> None:
    """Renamed into place, so a reader never sees a partial file."""
    write_sidecar(photo, FULL)
    assert list(photo.parent.glob("*.tmp")) == []
    assert sidecar_path(photo).is_file()


# ----------------------------------------------------------------------
# The bridge from the catalog
# ----------------------------------------------------------------------


def test_a_sidecar_is_built_from_what_the_catalog_knows(catalog: Catalog) -> None:
    location = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))
    session = catalog.sessions.add(Session(
        started_at=dt.datetime(2026, 9, 6, 10), event="Beach day",
        location_id=location.id,
    ))
    person = catalog.people.add(Person(name="Alice"))
    tag = catalog.tags.add(Tag(name="seaside"))
    photo = catalog.photos.add(Photo(
        content_hash="a" * 64, library_path="2026/2026-09-06/x.jpg",
        original_name="DSCF1.JPG",
        captured_at=dt.datetime(2026, 9, 6, 11), imported_at=dt.datetime(2026, 9, 20),
        rating=5, session_id=session.id,
    ))
    assert photo.id is not None
    catalog.photos.add_person(photo.id, person.id)
    catalog.photos.add_tag(photo.id, tag.id)

    assert sidecar_for(catalog, photo) == Sidecar(
        rating=5, location="St Aldo", people=("Alice",),
        event="Beach day", tags=("seaside",),
    )


def test_a_per_photo_location_overrides_its_session_s(catalog: Catalog) -> None:
    """FR-3.4's precedence, carried into the sidecar. Recording the session's
    location on a photograph that disagrees would write down something false."""
    session_place = catalog.locations.add(Location(name="St Aldo", slug="st-aldo"))
    other = catalog.locations.add(Location(name="Falmouth", slug="falmouth"))
    session = catalog.sessions.add(Session(
        started_at=dt.datetime(2026, 9, 6, 10), location_id=session_place.id,
    ))
    photo = catalog.photos.add(Photo(
        content_hash="b" * 64, library_path="2026/2026-09-06/y.jpg",
        original_name="DSCF2.JPG",
        captured_at=dt.datetime(2026, 9, 6, 11), imported_at=dt.datetime(2026, 9, 20),
        session_id=session.id,
    ))
    assert photo.id is not None
    catalog.photos.set_location(photo.id, other.id)

    assert sidecar_for(catalog, photo).location == "Falmouth"


def test_an_untagged_photograph_makes_an_almost_empty_sidecar(catalog: Catalog) -> None:
    photo = catalog.photos.add(Photo(
        content_hash="c" * 64, library_path="2026/2026-09-06/z.jpg",
        original_name="DSCF3.JPG",
        captured_at=dt.datetime(2026, 9, 6, 11), imported_at=dt.datetime(2026, 9, 20),
        rating=2,
    ))
    assert sidecar_for(catalog, photo) == Sidecar(rating=2)


# ----------------------------------------------------------------------
# The acceptance criterion: somebody else can read it
# ----------------------------------------------------------------------


def test_every_property_is_read_back_by_exiv2(photo: Path, pyexiv2) -> None:
    """The whole of D-1, checked against a different implementation.

    exiv2 is not ours, is written in C++, and is what digiKam uses. If it can
    see all six of §5.2's properties in a file this module wrote, the claim
    that these tags survive this application is evidence rather than intent.
    """
    write_sidecar(photo, Sidecar(
        rating=4, location="St Aldo", people=("Alice", "Carol"),
        event="Beach day", tags=("seaside", "golden hour"),
        latitude=50.20576, longitude=-5.48539,
    ))

    with pyexiv2.Image(str(sidecar_path(photo))) as image:
        xmp = image.read_xmp()

    assert xmp["Xmp.xmp.Rating"] == "4"
    assert xmp["Xmp.iptc.Location"] == "St Aldo"
    assert xmp["Xmp.photoshop.City"] == "St Aldo"
    assert xmp["Xmp.iptcExt.PersonInImage"] == ["Alice", "Carol"]
    assert xmp["Xmp.dc.subject"] == ["seaside", "golden hour"]
    # A language alternative, which is what the IPTC spec says Event is —
    # and the shape exiv2 reports proves it is one. Asserting only that the
    # text appears somewhere would pass for plain text too, which is a
    # different XMP type and not what a strict reader expects to find.
    assert xmp["Xmp.iptcExt.Event"] == {'lang="x-default"': "Beach day"}
    assert xmp["Xmp.exif.GPSLatitude"] == "50,12.3456N"
    assert xmp["Xmp.exif.GPSLongitude"] == "5,29.1234W"


def test_exiv2_finds_nothing_unexpected_in_an_empty_sidecar(photo: Path, pyexiv2) -> None:
    """The other half: we are not writing properties nobody asked for."""
    write_sidecar(photo, Sidecar())

    with pyexiv2.Image(str(sidecar_path(photo))) as image:
        assert image.read_xmp() == {"Xmp.xmp.Rating": "0"}

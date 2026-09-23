"""XMP sidecars — the half of D-1 that outlives this application.

The catalog is an index. If it is deleted, or this app is abandoned, or the owner
moves to Lightroom in three years, the thing that has to still be true is that
*the tags are attached to the photographs*. That is what a sidecar is for, and
it is why NFR-2 can call the database rebuildable at all.

A sidecar is written beside its photograph as ``<full filename>.xmp`` —
``2026-09-06_st-aldo_DSCF1024.jpg.xmp``, extension included — so a JPEG and the
RAF beside it never contend for one file (§5.2).

Four decisions worth the space.

**The XMP is written by hand, with the standard library.** Not for the sake of
it: the test suite reads what this writes back with **exiv2**, which is a
genuinely separate implementation and the one digiKam is built on. If this
module used exiv2 to write as well, that test would prove the library agrees
with itself and nothing about whether anyone else can read the file. Six fixed
properties of RDF/XML do not justify a compiled dependency, and the
verification is worth more than the convenience.

**Foreign properties survive a write.** If Lightroom has put a keyword or a
develop setting in a sidecar, rewriting the file from scratch would silently
delete it — which is the exact failure D-1 exists to prevent, committed by the
app that promised to prevent it. So a write parses whatever is already there,
replaces only the properties this app owns, and leaves the rest alone.

**A failed write is raised, never swallowed** (FR-5.6). Metadata that the user
believes is safe on disk, and is not, is worse than metadata they know they
still have to enter. Writes are atomic — a temporary file, fsynced, then
renamed — so a crash cannot leave a half-written sidecar that parses to
something almost right.

**Rating is always written; everything else only when it has a value.** An
absent ``Iptc4xmpCore:Location`` means "this app has nothing to say about the
location", which is different from an empty one meaning "the location is
blank". The rating is the exception because 0 is a real answer: unrated.
"""

from __future__ import annotations

import os
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported for typing only — sidecars.py owes the catalog
    from .catalog import Catalog  # nothing at runtime, and must not.
    from .models import Photo

__all__ = [
    "SidecarError",
    "Sidecar",
    "sidecar_for",
    "sidecar_path",
    "read_sidecar",
    "write_sidecar",
    "prepare_sidecar",
    "PendingSidecar",
    "render_xmp",
    "parse_xmp",
    "NAMESPACES",
]


class SidecarError(Exception):
    """A sidecar could not be written or could not be understood."""


#: The five vocabularies §5.2's properties come from, plus RDF itself. These
#: URIs are the actual identity of a property — the prefix is only a label, and
#: another application is free to use different ones — so everything here
#: matches on URI and never on prefix.
NAMESPACES = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "x": "adobe:ns:meta/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "Iptc4xmpCore": "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "exif": "http://ns.adobe.com/exif/1.0/",
}

_RDF = NAMESPACES["rdf"]
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def _q(prefix: str, name: str) -> str:
    return f"{{{NAMESPACES[prefix]}}}{name}"


#: Every property this app claims ownership of. A write replaces exactly these
#: and nothing else; anything not in this list belongs to whoever put it there.
_OWNED = (
    _q("xmp", "Rating"),
    _q("dc", "subject"),
    _q("photoshop", "City"),
    _q("Iptc4xmpCore", "Location"),
    _q("Iptc4xmpExt", "PersonInImage"),
    _q("Iptc4xmpExt", "Event"),
    _q("exif", "GPSLatitude"),
    _q("exif", "GPSLongitude"),
)


@dataclass(frozen=True, slots=True)
class Sidecar:
    """The tags that travel with a photograph (§5.2)."""

    #: 0-5 (FR-5.5). 0 is a real answer — unrated — so it is always written.
    rating: int = 0
    #: Written to both ``Iptc4xmpCore:Location`` and ``photoshop:City``,
    #: because different software looks in different places for the same idea.
    location: str | None = None
    people: tuple[str, ...] = ()
    event: str | None = None
    tags: tuple[str, ...] = ()
    #: Reserved for D-5's map. None today; here so adding one later does not
    #: mean rewriting every sidecar in the library.
    latitude: float | None = None
    longitude: float | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.rating <= 5:
            raise ValueError(f"rating must be 0-5, got {self.rating}")

    @property
    def empty(self) -> bool:
        """True when there is nothing here anyone would miss."""
        return self == Sidecar()


def sidecar_for(catalog: "Catalog", photo: "Photo") -> Sidecar:
    """Everything the catalog knows about a photograph, in sidecar form.

    The bridge between the two halves of D-1. Location follows FR-3.4's
    precedence — the per-photo override, else the session's — because that is
    the location that actually applies, and a sidecar recording the session's
    when the photograph disagrees would be a sidecar that is wrong.
    """
    assert photo.id is not None, "a photograph needs a row before it has a sidecar"
    location = catalog.photos.location(photo.id)
    session = None if photo.session_id is None else catalog.sessions.get(photo.session_id)
    return Sidecar(
        rating=photo.rating,
        location=None if location is None else location.name,
        people=tuple(person.name for person in catalog.photos.people(photo.id)),
        event=None if session is None else session.event,
        tags=tuple(tag.name for tag in catalog.photos.tags(photo.id)),
        latitude=None if location is None else location.latitude,
        longitude=None if location is None else location.longitude,
    )


def sidecar_path(photo_path: str | Path) -> Path:
    """``DSCF1024.jpg`` → ``DSCF1024.jpg.xmp``.

    The extension is kept rather than replaced, which is the whole reason a
    JPEG and its RAF can sit in one folder and still have a sidecar each
    (§5.2). ``DSCF1024.xmp`` for both would mean one of them silently wins.
    """
    path = Path(photo_path)
    if path.suffix.lower() == ".xmp":
        return path
    return path.with_name(path.name + ".xmp")


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingSidecar:
    """A sidecar written to a temporary file and not yet in place.

    The half of :func:`write_sidecar` that can fail, split out so a caller
    changing *several* sidecars at once can find out whether all of them will
    write before any of them does. Tagging a session touches 54 files, and the
    failure worth designing against is the thirtieth: without this, twenty-nine
    photographs would be left claiming a location the other twenty-five and the
    catalog disagree with.

    :meth:`commit` is a rename, which is atomic within a volume and as close to
    "cannot fail" as a filesystem offers.
    """

    destination: Path
    temporary: Path

    def commit(self) -> Path:
        try:
            os.replace(self.temporary, self.destination)
        except OSError as exc:
            raise SidecarError(f"{self.destination}: could not be put in place ({exc})") from exc
        return self.destination

    def discard(self) -> None:
        """Throw the temporary away. Never raises — this runs on a failure path."""
        try:
            self.temporary.unlink(missing_ok=True)
        except OSError:
            pass


def prepare_sidecar(photo_path: str | Path, sidecar: Sidecar) -> PendingSidecar:
    """Render and write a sidecar to a temporary, ready to be put in place.

    Everything expensive and everything fallible happens here: reading what is
    already there so foreign properties survive (§5.2), rendering, writing and
    fsyncing. What is left is a rename.
    """
    destination = sidecar_path(photo_path)
    try:
        existing = destination.read_text(encoding="utf-8") if destination.is_file() else None
    except OSError as exc:
        raise SidecarError(f"{destination}: cannot be read before rewriting ({exc})") from exc

    text = render_xmp(sidecar, existing=existing)

    # Named per thread as well as per process: two threads preparing the same
    # sidecar would otherwise delete each other's temporary (FR-2.3 learned the
    # same lesson about the thumbnail cache).
    temporary = destination.with_name(
        f"{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SidecarError(f"{destination}: could not be written ({exc})") from exc
    return PendingSidecar(destination, temporary)


def write_sidecar(photo_path: str | Path, sidecar: Sidecar) -> Path:
    """Write (or update) the sidecar beside *photo_path*. Returns its path.

    Atomic, and loud on failure (FR-5.6): the caller is told, so it can decide
    whether a photograph whose tags did not persist should count as saved.

    The two-phase version underneath leaves its temporary for the caller to
    dispose of, because a caller holding several of them wants to decide about
    all of them at once. A one-shot write has nothing to decide, so it cleans
    up after itself — a stray ``.tmp`` beside a photograph is litter in a
    folder success criterion 5 says should be *more* legible, not less.
    """
    pending = prepare_sidecar(photo_path, sidecar)
    try:
        return pending.commit()
    except BaseException:
        pending.discard()
        raise


def render_xmp(sidecar: Sidecar, *, existing: str | None = None) -> str:
    """The sidecar as an XMP packet.

    Given *existing* XMP, this edits it rather than replacing it: the owned
    properties are dropped and re-added, and everything else — other
    applications' namespaces, their properties, their attributes — is carried
    through untouched.
    """
    for prefix, uri in NAMESPACES.items():
        # Conventional prefixes, including ``x`` rather than a default
        # namespace. Semantically it makes no difference — a namespace is its
        # URI — but every other XMP writer emits ``x:xmpmeta``, and readers
        # that string-match rather than parse do exist.
        ET.register_namespace(prefix, uri)

    root, description = _document(existing)

    for owned in _OWNED:
        for element in description.findall(owned):
            description.remove(element)
        # A property can also arrive as an attribute — RDF allows both forms,
        # and some writers prefer the compact one. Ours has to go either way.
        description.attrib.pop(owned, None)

    _text(description, _q("xmp", "Rating"), str(sidecar.rating))
    if sidecar.location:
        _text(description, _q("photoshop", "City"), sidecar.location)
        _text(description, _q("Iptc4xmpCore", "Location"), sidecar.location)
    if sidecar.event:
        _lang_alt(description, _q("Iptc4xmpExt", "Event"), sidecar.event)
    if sidecar.people:
        _bag(description, _q("Iptc4xmpExt", "PersonInImage"), sidecar.people)
    if sidecar.tags:
        _bag(description, _q("dc", "subject"), sidecar.tags)
    if sidecar.latitude is not None:
        _text(description, _q("exif", "GPSLatitude"), _coordinate(sidecar.latitude, "NS"))
    if sidecar.longitude is not None:
        _text(description, _q("exif", "GPSLongitude"), _coordinate(sidecar.longitude, "EW"))

    ET.indent(root, space=" ")
    body = ET.tostring(root, encoding="unicode", xml_declaration=False)
    # The xpacket wrapper is what makes this a *packet* rather than merely a
    # file containing XMP; readers that scan for embedded XMP look for it.
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        f"{body}\n"
        '<?xpacket end="w"?>\n'
    )


def _document(existing: str | None) -> tuple[ET.Element, ET.Element]:
    """The ``x:xmpmeta`` and the ``rdf:Description`` to write into."""
    if existing:
        try:
            root = ET.fromstring(_strip_packet(existing))
        except ET.ParseError as exc:
            raise SidecarError(
                f"the existing sidecar is not valid XML and will not be overwritten "
                f"blindly ({exc})"
            ) from exc
        rdf = root.find(f"{{{_RDF}}}RDF")
        if rdf is None:
            raise SidecarError("the existing sidecar has no rdf:RDF element")
        description = rdf.find(f"{{{_RDF}}}Description")
        if description is None:
            description = ET.SubElement(rdf, f"{{{_RDF}}}Description")
            description.set(f"{{{_RDF}}}about", "")
        return root, description

    root = ET.Element(_q("x", "xmpmeta"))
    rdf = ET.SubElement(root, f"{{{_RDF}}}RDF")
    description = ET.SubElement(rdf, f"{{{_RDF}}}Description")
    description.set(f"{{{_RDF}}}about", "")
    return root, description


def _strip_packet(text: str) -> str:
    """Drop the ``<?xpacket?>`` processing instructions ElementTree will not eat."""
    lines = [
        line for line in text.splitlines()
        if not line.lstrip().startswith("<?xpacket")
    ]
    return "\n".join(lines)


def _text(parent: ET.Element, tag: str, value: str) -> None:
    ET.SubElement(parent, tag).text = value


def _bag(parent: ET.Element, tag: str, values: tuple[str, ...]) -> None:
    """An unordered set — what ``dc:subject`` and ``PersonInImage`` both are."""
    bag = ET.SubElement(ET.SubElement(parent, tag), f"{{{_RDF}}}Bag")
    for value in values:
        ET.SubElement(bag, f"{{{_RDF}}}li").text = value


def _lang_alt(parent: ET.Element, tag: str, value: str) -> None:
    """A language alternative. ``Iptc4xmpExt:Event`` is one, per the IPTC spec,
    and writing it as plain text makes readers that expect the right type show
    nothing at all."""
    alt = ET.SubElement(ET.SubElement(parent, tag), f"{{{_RDF}}}Alt")
    item = ET.SubElement(alt, f"{{{_RDF}}}li")
    item.set(_XML_LANG, "x-default")
    item.text = value


def _coordinate(value: float, hemispheres: str) -> str:
    """XMP writes coordinates as ``DDD,MM.mmmmH``, not as a decimal degree."""
    hemisphere = hemispheres[0] if value >= 0 else hemispheres[1]
    magnitude = abs(value)
    degrees = int(magnitude)
    minutes = (magnitude - degrees) * 60
    return f"{degrees},{minutes:.4f}{hemisphere}"


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def read_sidecar(path: str | Path) -> Sidecar | None:
    """The sidecar beside a photograph, or None if there is not one.

    Takes either the photograph's path or the sidecar's. Absence is not an
    error — most photographs will not have one until they are tagged.
    """
    target = sidecar_path(path)
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SidecarError(f"{target}: cannot be read ({exc})") from exc
    try:
        return parse_xmp(text)
    except SidecarError as exc:
        raise SidecarError(f"{target}: {exc}") from exc


def parse_xmp(text: str) -> Sidecar:
    """Read §5.2's properties out of an XMP packet.

    Tolerant about *where* it finds them: properties may be elements or
    attributes, and there may be several ``rdf:Description`` elements, which is
    how some writers split namespaces up. Anything it does not recognise is
    ignored rather than being an error — another application's properties are
    not this one's business to validate.
    """
    try:
        root = ET.fromstring(_strip_packet(text))
    except ET.ParseError as exc:
        raise SidecarError(f"not valid XML ({exc})") from exc

    sidecar = Sidecar()
    for description in root.iter(f"{{{_RDF}}}Description"):
        sidecar = _absorb(sidecar, description)
    return sidecar


def _absorb(sidecar: Sidecar, description: ET.Element) -> Sidecar:
    changes: dict[str, object] = {}

    def scalar(tag: str) -> str | None:
        element = description.find(tag)
        if element is not None and element.text is not None:
            return element.text.strip()
        value = description.attrib.get(tag)
        return value.strip() if value is not None else None

    rating = scalar(_q("xmp", "Rating"))
    if rating is not None:
        try:
            # A rating outside 0-5 is another application's idea, not a reason
            # to refuse the whole file. XMP's own -1 means "rejected".
            changes["rating"] = min(5, max(0, int(float(rating))))
        except ValueError:
            pass

    # Iptc4xmpCore:Location first: photoshop:City is the compatibility copy,
    # and if the two disagree the IPTC one is the one this app wrote.
    location = scalar(_q("Iptc4xmpCore", "Location")) or scalar(_q("photoshop", "City"))
    if location:
        changes["location"] = location

    event = _read_lang_alt(description, _q("Iptc4xmpExt", "Event")) or scalar(
        _q("Iptc4xmpExt", "Event")
    )
    if event:
        changes["event"] = event

    people = _read_list(description, _q("Iptc4xmpExt", "PersonInImage"))
    if people:
        changes["people"] = people

    tags = _read_list(description, _q("dc", "subject"))
    if tags:
        changes["tags"] = tags

    latitude = scalar(_q("exif", "GPSLatitude"))
    if latitude is not None:
        parsed = _parse_coordinate(latitude)
        if parsed is not None:
            changes["latitude"] = parsed
    longitude = scalar(_q("exif", "GPSLongitude"))
    if longitude is not None:
        parsed = _parse_coordinate(longitude)
        if parsed is not None:
            changes["longitude"] = parsed

    return replace(sidecar, **changes) if changes else sidecar  # type: ignore[arg-type]


def _read_list(description: ET.Element, tag: str) -> tuple[str, ...]:
    """The items of an ``rdf:Bag`` or ``rdf:Seq``, in document order.

    Both are accepted on read even though this app writes Bags: ``dc:subject``
    is specified as a Bag, but plenty of software writes a Seq, and refusing to
    read one would lose exactly the tags D-1 promises to keep.
    """
    container = description.find(tag)
    if container is None:
        return ()
    items = []
    for child in container:
        if child.tag in (f"{{{_RDF}}}Bag", f"{{{_RDF}}}Seq", f"{{{_RDF}}}Alt"):
            items.extend(
                item.text.strip()
                for item in child.findall(f"{{{_RDF}}}li")
                if item.text
            )
    return tuple(items)


def _read_lang_alt(description: ET.Element, tag: str) -> str | None:
    """The ``x-default`` of a language alternative, else the first entry."""
    container = description.find(tag)
    if container is None:
        return None
    alt = container.find(f"{{{_RDF}}}Alt")
    if alt is None:
        return None
    items = alt.findall(f"{{{_RDF}}}li")
    for item in items:
        if item.get(_XML_LANG) == "x-default" and item.text:
            return item.text.strip()
    return items[0].text.strip() if items and items[0].text else None


def _parse_coordinate(value: str) -> float | None:
    """``50,12.3456N`` back to ``50.20576``."""
    text = value.strip()
    if not text or text[-1] not in "NSEW":
        return None
    hemisphere, body = text[-1], text[:-1]
    parts = body.split(",")
    try:
        degrees = float(parts[0])
        minutes = float(parts[1]) if len(parts) > 1 else 0.0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
    except (ValueError, IndexError):
        return None
    magnitude = degrees + minutes / 60 + seconds / 3600
    return -magnitude if hemisphere in "SW" else magnitude

"""Build a Bluebeam Markups Summary XML mirroring the user-supplied sample.

Schema reference (from samples/bluebeam markup xml example.xml):
    <MarkupSummary Version="1.0" Document="...">
      <Total>
        <Subject>UC Artemis (N)</Subject>
      </Total>
      <Markup>
        <Subject>Callout</Subject>
        <Page_Label>...</Page_Label>
        <Comments>...</Comments>
        <Layer />
        <Author>UC Artemis</Author>
        <Document_Width>2.0000 in</Document_Width>
        <Document_Height>0.5000 in</Document_Height>
        <Status />
        <Color>#FF0000</Color>
        <Space />
        <Date>4/13/2026 3:53:02 PM</Date>
      </Markup>
      ...
    </MarkupSummary>
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

from core.pdf_writer import AUTHOR, WrittenAnnot


COLOR_HEX = "#FF0000"


def _pt_to_inches(pt: float) -> str:
    return f"{pt / 72.0:.4f} in"


def _format_date(dt) -> str:
    """Format a datetime in Bluebeam's M/D/YYYY h:MM:SS AM/PM style."""
    # %#I is Windows-only (no zero pad). Fall back to int() on platforms without it.
    hour = dt.hour % 12 or 12
    am_pm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.month}/{dt.day}/{dt.year} {hour}:{dt.minute:02d}:{dt.second:02d} {am_pm}"


def write_summary(
    annotations: Iterable[WrittenAnnot],
    document_filename: str,
    output_xml_path: str | Path,
) -> None:
    """Write the Markups Summary XML to disk."""
    annot_list = list(annotations)

    root = ET.Element("MarkupSummary", attrib={"Version": "1.0", "Document": document_filename})

    total = ET.SubElement(root, "Total")
    subject_total = ET.SubElement(total, "Subject")
    subject_total.text = f"{AUTHOR} ({len(annot_list)})"

    for a in annot_list:
        m = ET.SubElement(root, "Markup")
        ET.SubElement(m, "Subject").text = "Callout"
        ET.SubElement(m, "Page_Label").text = a.page_label
        ET.SubElement(m, "Comments").text = a.body_text
        ET.SubElement(m, "Layer")
        ET.SubElement(m, "Author").text = AUTHOR
        ET.SubElement(m, "Document_Width").text = _pt_to_inches(a.text_box_rect.width)
        ET.SubElement(m, "Document_Height").text = _pt_to_inches(a.text_box_rect.height)
        ET.SubElement(m, "Status")
        ET.SubElement(m, "Color").text = COLOR_HEX
        ET.SubElement(m, "Space")
        ET.SubElement(m, "Date").text = _format_date(a.created_at)

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    Path(output_xml_path).parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_xml_path, xml_declaration=True, encoding="utf-8")

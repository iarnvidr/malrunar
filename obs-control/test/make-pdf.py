# SPDX-License-Identifier: Apache-2.0
"""Write a small valid PDF: make-pdf.py <pages> <file>. A fixture for the smoke test."""
import sys

pages, path = int(sys.argv[1]), sys.argv[2]
objects = ["<< /Type /Catalog /Pages 2 0 R >>"]
objects.append("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % (3 + i) for i in range(pages)), pages))
objects += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 180] >>"] * pages
out, offsets = b"%PDF-1.4\n", []
for number, body in enumerate(objects, 1):
    offsets.append(len(out))
    out += ("%d 0 obj\n%s\nendobj\n" % (number, body)).encode()
xref = len(out)
out += ("xref\n0 %d\n" % (len(objects) + 1)).encode() + b"0000000000 65535 f \n"
out += b"".join(("%010d 00000 n \n" % offset).encode() for offset in offsets)
out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)).encode()
with open(path, "wb") as handle:
    handle.write(out)

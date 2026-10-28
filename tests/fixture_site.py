"""Local fixture literary-magazine site used by collector tests and the local proofs (change / breakage / PDF / image / pagination).
Synthetic content only; served on loopback."""
import http.server
import io
import threading

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def make_pdf(lines):
    """Minimal text PDF (Helvetica) built by hand: no third-party deps."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 12 Tf 50 780 Td 16 TL\n" + "\n".join("(%s) Tj T*" % esc(l) for l in lines) + "\nET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            "<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content), "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += ("%d 0 obj\n%s\nendobj\n" % (i, o)).encode("latin-1")
    x = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode() + b"".join(("%010d 00000 n \n" % o).encode() for o in offs)
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)).encode()
    return out


def make_png(lines, size=(1100, 420)):
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT, 34)
    y = 30
    for l in lines:
        d.text((40, y), l, fill="black", font=f)
        y += 60
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def make_scanned_pdf(lines):
    im = Image.open(io.BytesIO(make_png(lines, (1240, 700)))).convert("RGB")
    b = io.BytesIO()
    im.save(b, "PDF", resolution=150)
    return b.getvalue()


def page(title, body, extra_head=""):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><title>%s</title>%s</head><body><nav><a href='/'>Home</a> <a href='/submit'>Submissions</a> "
            "<a href='/calls/'>Open calls</a></nav>%s<footer>Follow us <a href='https://www.instagram.com/fixturereview'>Instagram</a></footer></body></html>" % (title, extra_head, body)).encode()


class State:
    version = 1
    hits = {}


def build(path):
    v = State.version
    if path == "/":
        return 200, "text/html", page("The Fixture Review", "<h1>The Fixture Review</h1><p>A literary magazine of fiction, poetry and book reviews. Published by Aurora Press Foundation.</p>"
                                      "<p><a href='/submit'>Submission guidelines</a> | <a href='/calls/'>Calls for submissions</a> | <a href='/about/payments'>Payments</a></p>")
    if path == "/submit":
        return 200, "text/html", page("Submission guidelines - The Fixture Review",
                                       "<h1>Submission guidelines</h1><p>The Fixture Review reads fiction, poetry and book reviews.</p>"
                                       "<p>Simultaneous submissions are welcome; please withdraw promptly if accepted elsewhere.</p>"
                                       "<p>We do not accept AI-generated work.</p><p>Open to writers worldwide.</p>")
    if path == "/about/payments":
        return 200, "text/html", page("Payments - The Fixture Review",
                                       "<h1>Payments and fees</h1><p>There is no submission fee. We pay $75 per poem and $150 per story.</p>")
    if path == "/calls/":
        return 200, "text/html", page("Calls for submissions - The Fixture Review",
                                       "<h1>Calls for submissions</h1><ul><li><a href='/calls/2026-fall'>Fall 2026 Fiction Issue - call for submissions</a></li></ul>"
                                       "<a rel='next' href='/calls/page/2/'>Older calls</a>")
    if path == "/calls/page/2/":
        return 200, "text/html", page("Calls for submissions (page 2) - The Fixture Review",
                                       "<h1>More calls</h1><ul><li><a href='/files/guidelines.pdf'>Poetry Prize guidelines (PDF)</a></li>"
                                       "<li><a href='/files/notice.png'>Translation Feature notice (image)</a></li>"
                                       "<li><a href='/calls/2024-spring'>Spring 2024 call for submissions</a></li></ul>")
    if path == "/calls/2024-spring":
        return 200, "text/html", page("Spring 2024 call for submissions", "<h1>Spring 2024 call for submissions</h1><p>Submissions close March 15, 2024.</p>")
    if path == "/calls/2026-fall":
        if v == 1:
            body = "<h1>Fall 2026 Fiction Issue - call for submissions</h1><p>Submissions are now open. Deadline: November 30, 2026.</p><p>Fiction up to 5,000 words.</p>"
        elif v == 2:
            body = ("<h1>Fall 2026 Fiction Issue - call for submissions</h1><p>Submissions are now open. The deadline has been extended to December 20, 2026.</p>"
                    "<p>Fiction up to 5,000 words.</p>")
        else:       # v3: template redesign, the facts moved into script-rendered content: structure collapses
            body = "<div id='app'></div><script>window.__DATA__={}</script><p>Loading calls...</p><p>Check back soon.</p>"
        return 200, "text/html", page("Fall 2026 Fiction Issue - call for submissions", body)
    if path == "/files/guidelines.pdf":
        return 200, "application/pdf", make_pdf(["Poetry Prize 2026 - Guidelines", "Submission deadline: January 31, 2027.", "Entry fee: $15 per entry.",
                                                  "Simultaneous submissions are not accepted.", "Open to residents of the United States and Canada."])
    if path == "/files/notice.png":
        return 200, "image/png", make_png(["Translation Feature 2026", "Submissions close December 15, 2026", "Submission fee: none", "Open to all translators"])
    if path == "/sitemap.xml":
        base = "http://%s" % State.host
        urls = ["/", "/submit", "/about/payments", "/calls/", "/calls/2026-fall", "/files/guidelines.pdf"]
        return 200, "application/xml", ("<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>%s</urlset>" %
                                        "".join("<url><loc>%s%s</loc></url>" % (base, u) for u in urls)).encode()
    if path == "/robots.txt":
        return 200, "text/plain", ("User-agent: *\nDisallow: /private/\nSitemap: http://%s/sitemap.xml\n" % State.host).encode()
    return 404, "text/plain", b"not found"


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        State.hits[self.path] = State.hits.get(self.path, 0) + 1
        code, ctype, body = build(self.path.split("?")[0])
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    State.host = "127.0.0.1:%d" % srv.server_address[1]
    State.version = 1
    State.hits = {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

import markdown
from weasyprint import HTML

with open('/ssd/benchmark/docs/iouring_ring_buffers_explained.md', 'r') as f:
    md_text = f.read()

html_body = markdown.markdown(md_text, extensions=['tables', 'fenced_code', 'codehilite'])

html_full = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@page {{
    size: A4;
    margin: 20mm 18mm 20mm 18mm;
}}
body {{
    font-family: 'DejaVu Sans', Arial, sans-serif;
    font-size: 11.5px;
    line-height: 1.5;
    color: #222;
}}
h1 {{
    font-size: 20px;
    border-bottom: 2px solid #333;
    padding-bottom: 8px;
    margin-top: 0;
}}
h2 {{
    font-size: 16px;
    border-bottom: 1px solid #ccc;
    padding-bottom: 5px;
    margin-top: 28px;
    color: #1a1a2e;
}}
h3 {{
    font-size: 13px;
    margin-top: 18px;
}}
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
    font-size: 10.5px;
    table-layout: auto;
    border: 2px solid #333;
}}
th, td {{
    border: 1px solid #333;
    padding: 5px 7px;
    text-align: left;
    word-wrap: break-word;
    overflow-wrap: break-word;
}}
th {{
    background-color: #e8eaf6;
    font-weight: bold;
    color: #1a1a2e;
    border-bottom: 2px solid #333;
}}
tr:nth-child(even) {{
    background-color: #fafafa;
}}
code {{
    background: #f0f0f0;
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 10px;
    font-family: 'DejaVu Sans Mono', monospace;
    word-wrap: break-word;
}}
pre {{
    background: #f4f4f4;
    padding: 10px;
    border-radius: 5px;
    font-size: 9.5px;
    white-space: pre-wrap;
    word-wrap: break-word;
    border-left: 3px solid #3f51b5;
    line-height: 1.45;
}}
pre code {{
    background: none;
    padding: 0;
    white-space: pre-wrap;
    word-wrap: break-word;
}}
p {{
    margin: 6px 0;
}}
strong {{
    color: #1a237e;
}}
</style></head><body>{html_body}</body></html>"""

out_path = '/ssd/benchmark/docs/кольцевой_буфер.pdf'
HTML(string=html_full).write_pdf(out_path)
print(f"Saved: {out_path}")

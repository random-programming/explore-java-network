import markdown
from weasyprint import HTML

with open('/ssd/benchmark/docs/methodology_v4.md', 'r') as f:
    md_text = f.read()

html_body = markdown.markdown(md_text, extensions=['tables', 'fenced_code', 'codehilite'])

html_full = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: 'DejaVu Sans', Arial, sans-serif; margin: 40px; font-size: 13px; line-height: 1.6; color: #222; }}
h1 {{ font-size: 22px; border-bottom: 2px solid #333; padding-bottom: 8px; }}
h2 {{ font-size: 18px; border-bottom: 1px solid #ccc; padding-bottom: 5px; margin-top: 30px; }}
h3 {{ font-size: 15px; margin-top: 20px; }}
table {{ border-collapse: collapse; width: 100%; margin: 15px 0; font-size: 11px; table-layout: fixed; }}
th, td {{ border: 1px solid #bbb; padding: 6px 8px; text-align: left; word-wrap: break-word; overflow-wrap: break-word; }}
th {{ background-color: #f0f0f0; font-weight: bold; }}
tr:nth-child(even) {{ background-color: #fafafa; }}
code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 11px; word-wrap: break-word; }}
pre {{ background: #f4f4f4; padding: 12px; border-radius: 5px; font-size: 10px; white-space: pre-wrap; word-wrap: break-word; }}
pre code {{ white-space: pre-wrap; word-wrap: break-word; }}
</style></head><body>{html_body}</body></html>"""

HTML(string=html_full).write_pdf('/ssd/benchmark/docs/methodology_v4.pdf')
print("Saved: /ssd/benchmark/docs/methodology_v4.pdf")

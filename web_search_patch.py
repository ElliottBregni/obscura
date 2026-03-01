filepath = "/Users/elliottbregni/dev/obscura-main/obscura/tools/system/__init__.py"
with open(filepath, "r") as f:
    src = f.read()

old_fn = 'async def web_search(query: str, max_results: int = 5) -> str:\n    limit = max(1, min(int(max_results), 20))\n    encoded = url_parse.quote_plus(query)\n    endpoint = (\n        "https://api.duckduckgo.com/"\n        f"?q={encoded}&format=json&no_redirect=1&no_html=1&skip_disambig=1"\n    )'

if old_fn in src:
    print("FOUND old function")
else:
    print("NOT FOUND - checking first 200 chars of web_search def")
    idx = src.find("async def web_search")
    print(repr(src[idx:idx+300]))

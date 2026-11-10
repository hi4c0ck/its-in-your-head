import io, os

spec_path = r'D:\work\its-in-your-head\.agnes\work\gif_blackfold\spec_extract.json'
uri_path  = r'D:\work\its-in-your-head\.agnes\work\gif_blackfold\uri_extract.txt'
pane_path = r'D:\work\its-in-your-head\desktop\brick-mask.html'

spec = io.open(spec_path, encoding='utf-8').read().strip()
uri  = io.open(uri_path,  encoding='utf-8').read().strip()
h    = io.open(pane_path, encoding='utf-8').read()

h = h.replace('__SPEC_JSON__', spec)
h = h.replace('"__PORTRAIT_URI__"', '"' + uri + '"')

# verify
assert '__SPEC_JSON__' not in h, 'SPEC token still present'
assert '__PORTRAIT_URI__' not in h, 'URI token still present'
assert '"accent":"#35C8FF"' in h, 'SPEC not inlined'
assert 'data:image/svg+xml' in h, 'URI not inlined'

io.open(pane_path, 'w', encoding='utf-8').write(h)
io.open(r'D:\work\its-in-your-head\.agnes\work\gif_blackfold\diag.txt', 'w').write(
    'OK bytes=' + str(len(h)) + ' spec_len=' + str(len(spec)) + ' uri_len=' + str(len(uri))
)

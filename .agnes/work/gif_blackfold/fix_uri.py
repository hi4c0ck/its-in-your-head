import io

uri = io.open(r'D:\work\its-in-your-head\.agnes\work\gif_blackfold\uri_extract.txt', encoding='utf-8').read().strip()
assert uri.startswith('data:image/svg+xml,'), 'extract not a data URI'
pane = r'D:\work\its-in-your-head\desktop\brick-mask.html'
lines = io.open(pane, encoding='utf-8').read().split('\n')
out = []
replaced = False
for ln in lines:
    if ln.startswith('const PORTRAIT_URI = '):
        out.append('const PORTRAIT_URI = "' + uri + '";')
        replaced = True
    else:
        out.append(ln)
assert replaced, 'did not find the PORTRAIT_URI line to replace'
io.open(pane, 'w', encoding='utf-8').write('\n'.join(out))
io.open(r'D:\work\its-in-your-head\.agnes\work\gif_blackfold\diag.txt','w').write(
    'replaced PORTRAIT_URI with real one, uri_len=' + str(len(uri)))

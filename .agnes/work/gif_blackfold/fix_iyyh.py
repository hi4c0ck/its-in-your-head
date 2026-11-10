import io

uri = io.open(r'D:\work\its-in-your-head\.agnes\work\gif_blackfold\uri_extract.txt', encoding='utf-8').read().strip()
assert uri.startswith('data:image/svg+xml,'), 'extract is not a data URI'
pane = r'D:\work\its-in-your-head\desktop\brick-mask.html'
lines = io.open(pane, encoding='utf-8').read().split('\n')
out = []
replaced = False
for ln in lines:
    if '__IIYH_URI__' in ln and 'const IIYH' in ln:
        out.append('const IIYH="' + uri + '";   // the real IiYH pencil portrait (data URI)')
        replaced = True
    else:
        out.append(ln)
assert replaced, 'did not find the IIYH token line to replace'
io.open(pane, 'w', encoding='utf-8').write('\n'.join(out))
io.open(r'D:\work\its-in-your-head\.agnes\work\gif_blackfold\diag.txt', 'w').write('IIYH uri inlined, len=' + str(len(uri)))

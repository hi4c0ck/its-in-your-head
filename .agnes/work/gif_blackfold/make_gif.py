import subprocess, os, sys

FF = r"C:\Users\user\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
SRC = r"C:\Users\user\Downloads\hs.mp4"
OUT = r"C:\Users\user\Downloads\hs.gif"
PAL = r"C:\Users\user\Downloads\.hs_gif.pal.png"
FPS = 12       # step-down 30->12: chunkier bit-step feel, ~2.5x fewer frames
MAXC = 128
KEY = "colorkey=0x000000:0.03:0.12"  # fold black -> transparent

def run(args):
    r = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y"] + args,
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(1)

# pass 1: palette from the keyed (transparent-black) stream
run(["-i", SRC, "-vf",
     f"fps={FPS},{KEY},palettegen=max_colors={MAXC}", PAL])
# pass 2: encode with that palette -> single transparent index for black
run(["-i", SRC, "-i", PAL,
     "-filter_complex",
     f"[0:v]fps={FPS},{KEY}[v];[v][1:v]paletteuse=dither=none:alpha_threshold=128",
     "-loop", "0", OUT])

os.remove(PAL)
sz = os.path.getsize(OUT)
print(f"OK {OUT} {sz/1024:.1f} KB")

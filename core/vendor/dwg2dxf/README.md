# Bundled `dwg2dxf` (GNU LibreDWG)

Prism ships `dwg2dxf` so a customer's `.dwg` converts to `.dxf` for BOQ
measurement and AI preview without installing anything — the ODA File
Converter (odaconverter.com) is still tried first when it's present; this is
the fallback for machines that don't have it. `find_dwg_converter()` in
`prism_terminal/core/boq.py` installs the right platform copy into
`~/.prism/tools/dwg2dxf/` the first time it's needed, and finds it there on
every run after.

LibreDWG is GPL-3.0-or-later. These binaries are redistributed unmodified
except for one relinking step on the macOS build (below). This file is
Prism's source offer for the exact versions bundled.

## What's here, and where it came from

### `win64/` — version 0.14.8739

Downloaded, as-is, from LibreDWG's own GitHub release — not a third-party
build:

    https://github.com/LibreDWG/libredwg/releases/download/0.14.8739/libredwg-0.14.8739-win64.zip

GitHub's own content digest for that exact file (verified against it at
download time, 2 Nov 2026):

    sha256:8520a3d90eb26a6de431b04bfb44480f53f13b40fa2800d6de9af00bf17d6416

`dwg2dxf.exe`, `libredwg-0.dll`, `libiconv-2.dll`, `libpcre2-16-0.dll` and
`libpcre2-8-0.dll` are the five files from that zip's top level — everything
`dwg2dxf.exe` needs is a sibling file, Windows' own DLL search order finds
them with no install step, no registry entry, no admin rights.

Source for this exact version:
    https://github.com/LibreDWG/libredwg/releases/download/0.14.8739/libredwg-0.14.8739.tar.gz
(sha256: cc68aa568333ff3f603e474f9260a629a5c6ba8f6502398708c92f7265962c1b, from
the release's own `dist.sha256`)

### `macos-arm64/` — version 0.13.3

LibreDWG publishes no official macOS binary at all. `dwg2dxf` here is
Homebrew's own `libredwg` 0.13.3 bottle (`brew install libredwg`), copied
unmodified, plus ONE change: its reference to `libredwg.0.dylib` was
rewritten from the Homebrew Cellar's absolute path to `@loader_path/…` with
`install_name_tool`, and both files were then ad-hoc re-signed
(`codesign -s -`), so the pair runs standalone — tested by running it with
`PATH=/usr/bin:/bin` and no `DYLD_*`/Homebrew in the environment at all, and
by diffing its output against the unmodified Homebrew copy's on a real,
complex architectural .dwg (byte-identical). No other change.

Apple Silicon (arm64) only — there is no bundled copy for Intel Macs; on one,
`find_dwg_converter()` falls through to the ODA File Converter / a
`brew install libredwg` the customer installs themselves, same as before this
existed.

Source: Homebrew's own formula points at LibreDWG's official release
tarball for 0.13.3:
    https://github.com/LibreDWG/libredwg/releases/download/0.13.3/libredwg-0.13.3.tar.gz

## A real failure mode, found while verifying this — read before changing the caller

A real, complex architectural `.dwg` converted here (lots of AEC extension
objects LibreDWG doesn't fully decode) produced a `.dxf` that `dwg2dxf`
itself reported success on (exit 0, file written, `SECTION`/`ENDSEC`/`EOF`
all present) but that **`ezdxf.readfile()` — and even `ezdxf.recover.readfile()`
— refuse to open** (`DXFStructureError: Invalid tag (code=41, value="")`).
A clean exit code and a well-formed-looking file are not proof the DXF is
actually readable.

`core/boq.py`'s `dwg_to_dxf()` already guards against exactly this — it
opens the result with `_read_dxf()` before trusting it, and retries with a
minimal (`-m`) conversion if that fails — so nothing about this needed
changing there; this note exists so the next person touching that validation
step knows it is load-bearing, not defensive boilerplate, and has a real
reproduction case for why.

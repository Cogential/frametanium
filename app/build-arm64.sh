#!/bin/sh
# Cross-compiles the headset app for the Steam Frame (ARM64 Linux) into ../dist/app.
# Needs the NDK clang, the Ubuntu 24.04 arm64 sysroot and our SDL2 build (see the Lighthouse CLAUDE.md).
set -e
cd "$(dirname "$0")"
LLVM=${LLVM:-$HOME/Android/Sdk/ndk/28.2.13676358/toolchains/llvm/prebuilt/linux-x86_64/bin}
SYSROOT=${SYSROOT:-/data/cogent/sysroots/noble-arm64}
SDL=${SDL:-/data/cogent/sysroots/sdl2-arm64}
OUT=../dist/app
mkdir -p "$OUT/lib"
"$LLVM/clang" --target=aarch64-linux-gnu --sysroot="$SYSROOT" -fuse-ld=lld -O2 -Wall -march=armv8.2-a \
    -I"$SDL/include/SDL2" -D_REENTRANT app.c -L"$SDL/lib" -lSDL2-2.0 -Wl,-rpath,'$ORIGIN/lib' \
    -o "$OUT/port-manager"
"$LLVM/llvm-strip" "$OUT/port-manager"
cp -L "$SDL/lib/libSDL2-2.0.so.0" "$OUT/lib/"
cat > "$OUT/run.sh" <<'RUN'
#!/bin/sh
# Steam launches this. FrameDrop-style installs lose the executable bit, so restore it first.
cd "$(dirname "$0")" || exit 1
chmod +x ./port-manager 2>/dev/null
exec ./port-manager "$@"
RUN
chmod +x "$OUT/run.sh"
echo "built $OUT"

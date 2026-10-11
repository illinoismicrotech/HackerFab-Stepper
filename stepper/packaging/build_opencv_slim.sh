#!/usr/bin/env bash
# Builds a slim OpenCV Python module for Windows (cv2.cp312-win_amd64.pyd, ~15 MB instead of the
# 75 MB opencv-python one plus its 30 MB FFmpeg DLL). It contains only what the stepper uses:
#   core, imgproc (resize, colour conversion, Sobel, blur), imgcodecs (PNG/JPEG), and videoio with
#   the DirectShow backend (how the app talks to the Arducam on Windows).
# Media Foundation is not included (it does not build with MinGW, and the Arducam only accepts
# exposure changes through DirectShow anyway).
#
# Cross-compiled on Linux (Debian/Ubuntu) with MinGW-w64, against a Windows Python 3.12 install
# (only its include/, libs/ and numpy headers are used):
#   sudo apt install g++-mingw-w64-x86-64-posix cmake ninja-build nasm git
#   WINPY=/path/to/windows/python312 ./packaging/build_opencv_slim.sh
# Result: packaging/opencv-slim/cv2.cp312-win_amd64.pyd. build_exe.ps1 uses it automatically
# when it exists; otherwise the exe is built with the regular opencv-python wheel (bigger).
set -euo pipefail
VERSION=${OPENCV_VERSION:-4.14.0}
WINPY=${WINPY:?set WINPY to a Windows Python 3.12 folder (with include/, libs/ and numpy installed)}
HERE=$(cd "$(dirname "$0")" && pwd)
WORK=${WORK:-$HOME/opencv-slim-build}
mkdir -p "$WORK"
cd "$WORK"
[ -d opencv ] || git clone -q --depth 1 --branch "$VERSION" https://github.com/opencv/opencv.git opencv
cat > mingw.cmake <<'EOF'
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR AMD64)
set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc-posix)
set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++-posix)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
EOF
HOSTPY=$(command -v python3.12 || command -v python3)
mkdir -p build && cd build
cmake -G Ninja ../opencv \
  -DCMAKE_TOOLCHAIN_FILE="$WORK/mingw.cmake" -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_LIST=core,imgproc,imgcodecs,videoio,python3 -DBUILD_SHARED_LIBS=OFF \
  -DOPENCV_SKIP_PYTHON_LOADER=ON -DPYTHON3_CVPY_SUFFIX=.cp312-win_amd64.pyd \
  -DPYTHON3_EXECUTABLE="$HOSTPY" -DPYTHON_DEFAULT_EXECUTABLE="$HOSTPY" \
  -DPYTHON3_INCLUDE_DIR="$WINPY/include" -DPYTHON3_LIBRARY="$WINPY/libs/python312.lib" \
  -DPYTHON3_NUMPY_INCLUDE_DIRS="$WINPY/Lib/site-packages/numpy/_core/include" \
  -DPYTHON3_PACKAGES_PATH="$WORK/site" -DBUILD_opencv_python2=OFF \
  -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_DOCS=OFF -DBUILD_opencv_apps=OFF \
  -DBUILD_JAVA=OFF -DBUILD_opencv_js=OFF \
  -DWITH_DSHOW=ON -DWITH_MSMF=OFF -DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_OBSENSOR=OFF \
  -DWITH_IPP=OFF -DWITH_ITT=OFF -DWITH_OPENCL=OFF -DWITH_VA=OFF -DWITH_VA_INTEL=OFF -DWITH_LAPACK=OFF \
  -DWITH_EIGEN=OFF -DWITH_PROTOBUF=OFF -DWITH_OPENMP=OFF -DWITH_TBB=OFF -DWITH_CUDA=OFF -DWITH_VTK=OFF \
  -DWITH_QT=OFF -DWITH_GTK=OFF -DWITH_WIN32UI=OFF -DWITH_DIRECTX=OFF -DWITH_ADE=OFF -DWITH_FLATBUFFERS=OFF \
  -DWITH_KLEIDICV=OFF -DWITH_CAROTENE=OFF -DWITH_OPENVINO=OFF -DWITH_ONNX=OFF -DWITH_IMGCODEC_GIF=OFF \
  -DWITH_TIFF=OFF -DWITH_WEBP=OFF -DWITH_OPENJPEG=OFF -DWITH_JASPER=OFF -DWITH_OPENEXR=OFF -DWITH_AVIF=OFF \
  -DWITH_JPEGXL=OFF -DWITH_GDAL=OFF -DWITH_GDCM=OFF -DWITH_IMGCODEC_HDR=OFF -DWITH_IMGCODEC_SUNRASTER=OFF \
  -DWITH_IMGCODEC_PXM=OFF -DWITH_IMGCODEC_PFM=OFF -DWITH_SPNG=OFF \
  -DWITH_PNG=ON -DBUILD_PNG=ON -DWITH_JPEG=ON -DBUILD_JPEG=ON -DBUILD_ZLIB=ON \
  -DCPU_BASELINE=SSE3 "-DCPU_DISPATCH=SSE4_1;SSE4_2;AVX;FP16;AVX2" -DENABLE_PRECOMPILED_HEADERS=OFF \
  -DPARALLEL_ENABLE_PLUGINS=OFF -DVIDEOIO_ENABLE_PLUGINS=OFF -DHIGHGUI_ENABLE_PLUGINS=OFF \
  -DOPENCV_GENERATE_SETUPVARS=OFF \
  "-DCMAKE_SHARED_LINKER_FLAGS=-static -static-libgcc -static-libstdc++ -s" \
  "-DCMAKE_MODULE_LINKER_FLAGS=-static -static-libgcc -static-libstdc++ -s"
ninja
mkdir -p "$HERE/opencv-slim"
cp lib/python3/cv2.cp312-win_amd64.pyd "$HERE/opencv-slim/"
echo "Built $HERE/opencv-slim/cv2.cp312-win_amd64.pyd"

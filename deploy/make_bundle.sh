#!/bin/sh
# 在 Mac/开发机上运行：把网关代码打包成可拷贝到板子的压缩包。
# 用法：
#   sh deploy/make_bundle.sh [输出路径]
# 默认输出到仓库同级目录的 mili-gateway-bundle.tar.gz（不污染 git 仓库）。
set -e

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="${1:-$ROOT/../mili-gateway-bundle.tar.gz}"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/mili-gateway/stubs/argon2"

# 网关代码
cp "$ROOT/gateway.py" \
   "$ROOT/protocol.py" \
   "$ROOT/rtsp_inproc.py" \
   "$ROOT/rtsp_light.py" \
   "$ROOT/local_io.py" \
   "$ROOT/unlock_test.py" \
   "$STAGE/mili-gateway/"

# 部署文件
cp "$ROOT/deploy/start_gateway.sh" \
   "$ROOT/deploy/setup_board.sh" \
   "$ROOT/deploy/gateway.env.example" \
   "$ROOT/deploy/broker.yaml" \
   "$ROOT/deploy/S99mili" \
   "$ROOT/deploy/gateway.service" \
   "$ROOT/deploy/gateway.init" \
   "$ROOT/deploy/README.md" \
   "$STAGE/mili-gateway/"

# 兼容 LOCAL_MQTT_BROKER=1 旧部署所需的 argon2 导入 stub
cp "$ROOT/deploy/stubs/argon2/__init__.py" \
   "$ROOT/deploy/stubs/argon2/exceptions.py" \
   "$STAGE/mili-gateway/stubs/argon2/"

tar -czf "$OUT" -C "$STAGE" mili-gateway
echo "打包完成: $OUT"
echo "传到板子后："
echo "  mkdir -p /opt/mili-gateway"
echo "  tar xzf mili-gateway-bundle.tar.gz -C /opt/mili-gateway --strip-components=1"

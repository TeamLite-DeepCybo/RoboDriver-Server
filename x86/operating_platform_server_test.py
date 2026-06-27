import sys
import os

# 确保 internal_sync 模块可导入
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

# 导入 Flask server
_server_dir = os.path.join(os.path.dirname(_this_dir), "test", "demo1")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

import operating_platform_server

if __name__ == '__main__':
    operating_platform_server.init_streams()
    operating_platform_server.app.run(
        host="0.0.0.0",
        port=8088,
        debug=False,
        threaded=True,
    )

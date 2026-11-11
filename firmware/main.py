# 开机入口：只负责启动，全部逻辑都在 epaper_main.py。
#
# 以前 main.py 是 epaper_main.py 的整份复制（70KB），结果改 epaper_main.py
# 上传后「看起来成功、SHA1 也一致」，但开机跑的还是 main.py 那份旧代码，
# 修复永远不生效。现在只剩这一层薄壳，改逻辑只动 epaper_main.py。
import epaper_main

epaper_main.main()

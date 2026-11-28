# 模块执行入口只委托 CLI；导入包本身不启动 App、线程或主循环。
from kaki.cli import main

main()

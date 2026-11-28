---
name: kaki-devbox-build
description: 通过已配置SSH Devbox准备隔离源码、编译和测试，并取回远端提交、日志与退出码；用于本机缺少Linux/Blade/BPT环境的Media Platform或C++ SDK验证。不部署服务、不发布SDK、不修改远端既有工作区或保护分支。
metadata:
  kaki:
    required_tools: [kaki_command, kaki_capabilities]
    required_commands: [devbox-inspect, devbox-prepare, devbox-dependencies, devbox-go-cache, devbox-build, devbox-status, devbox-cancel]
---

先调用 `kaki_capabilities` 确认当前配置的 Devbox profiles，再执行
`kaki_command(name="devbox-inspect")`。主机和用户由本机可信配置固定，
不把SSH命令、主机地址或口令作为模型自由拼接的参数。

以实际结果确认连接用户、Linux架构、Git/Go/Blade/BPT/编译器位置和已有
代码仓位置。检查只读取元数据，不修改远端代码、分支或系统环境。
连接失败时区分网络、host key和认证错误；不复制私钥或Token，不自动开启
agent forwarding，不修改全局SSH/Go配置来绕过问题。

编译前记录本地已提交、已推送的精确SHA及对应新分支；SDK与Media Platform
分别验证，保留master、release和用户现有分支。使用专用隔离源码目录，
不在已有脏工作区reset、stash或覆盖。后台任务使用固定job ID，结果不明
先查同一job，不重复启动；编译/测试退出0与SDK发布、部署、MR合入分别判断。

SDK先验证原生库构建与回调测试，再验证Media Platform；若需要联动新SDK，
只使用隔离验证modfile/环境覆盖，不改正式依赖或发布版本。保留完整日志，
报告source commit、实际命令、工具链、退出码、测试范围和未验证项。

Devbox下载SDK的固定Git依赖发生publickey认证失败时，在本机SDK
项目调用`devbox-dependencies --task <同一task>`：只读取BUILD声明的固定版本，
通过本机现有认证生成bundle，导入专用目录中的只读源镜像。随后以新job重试构建；
Git地址替换仅作用于构建进程，不传私钥/Token、不修改全局Git配置。

Media因Devbox私有Go模块认证缺失而停在下载时，调用
`devbox-go-cache --task <task>`，仅从本机准备go.mod/go.sum声明的模块物料，
上传并校验归档后使用任务私有file GOPROXY。本机只下载源码，不编译业务。
需要中止旧构建时用`devbox-cancel --task <task> --job <job>`；它核对owner、
worker与child进程组后只终止该job。确认旧任务终态后再用新job验证。

操作参数与恢复规则见[编译工作流](references/workflow.md)，需要准备、启动或
恢复远端任务时读取。先SDK构建/测试，再Media联动；主Agent、模型凭据和SQLite
仍在本机，Devbox只接收已提交源码、编译命令及状态数据。

Media原生构建前，先用devbox-build的media-deps模式按仓库build.sh锁定模块准备ONNX/WB依赖；SDK原生测试、Media CGO=1编译/ELF解析与Makefile CGO=0单测分开报告，不把Stub测试当生产原生运行。

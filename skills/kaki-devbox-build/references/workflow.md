# Devbox编译工作流

主机和用户由可信profile环境KAKI_DEVBOX_TARGET指定；模型参数没有自由SSH命令、主机或系统安装入口。

1. `devbox-inspect`确认Linux、Go/Blade路径及连接身份。当前探查脚本兼容系统Python3.5；BPT没有独立binary不代表Blade插件不可用。扫描受限时不能断言没有仓库。
2. 在对应本机单Git项目调用`devbox-prepare`：

```json
{"repo":"sdk","branch":"codex/rtc-minus999-native","commit":"<40位已推送SHA>","task":"rtc-validation-<日期>"}
```

以上转为arguments数组：`["--repo","sdk","--branch","codex/rtc-minus999-native","--commit","<SHA>","--task","<task>"]`。
脚本只从当前匹配的本机仓创建Git bundle，经SSH/SCP传给用户自己的Devbox，校验SHA256后克隆到`~/.kaki-devbox/workspaces/<task>/<repo-name>`，创建独立codex验证分支。不复制密钥、不要求Devbox能访问Codebase、不修改既有远端代码或分支。源码仅包含选定提交；未提交文件不会传输。已有目录无owner标记、HEAD不同或tracked文件脏时拒绝覆盖，换新task目录。

3. 启动后台任务：`devbox-build`。

若Devbox对cpputil/log@1.0.0-streamlog、cpputil/metrics_sdk@v1.3.13、cpputil/program@1.0.0以及传递依赖cpputil/metrics2@dev、log_market/CppLogSDK@master、cpputil/consul@1.0.0、inf/monitoring-commons-cpp@v1.0.30、cpputil/env@master拉取报publickey认证失败，在本机SDK项目调用`devbox-dependencies`，arguments=`["--task","<task>"]`。适配器先按父依赖顺序核对本机SDK与固定log/program/metrics_sdk依赖的BUILD声明，再用本机现有Git读取权限制作指定版本bundle并逐项验SHA，导入task下的`.git-dependencies`。worker仅在构建子进程中设置Git URL重写，兼容Git2.29，不传凭据、不改全局Git配置。依赖回执记录repo/ref/qualified_ref/commit/bundle_sha256/path；同名分支/tag优先分支，按git clone -b语义选择并记完整refs路径；本机也无读取权限时明确失败，不绕过ACL。然后使用新job ID重试构建，保留旧失败记录。

```text
--repo sdk --task <task> --mode sdk-build --job sdk-build-<sha8> --timeout 1800
--repo sdk --task <task> --mode sdk-test --job sdk-test-<sha8> --timeout 1800
```

`sdk-build`执行`bash ./build.sh`；`sdk-test`执行`bash ./build.sh test`。首次在专用根初始化Blade，输出只进入专用工作区。后台worker有进程组和超时上限；同一任务根的构建串行。job ID固定，同参数再次start返回既有状态，不重复编译；不同mode不能复用同一job。

旧项目tarfile下的Blade可能排在PATH最前且不支持--enable-bpt。若主机已有官方/opt/tiger/typhoon-blade/blade，构建进程优先使用它并在专用根重新blade init，记录实际blade_path；不更新或覆盖机器的旧Blade，不修改全局Shell配置。BPT依赖由新版Blade按仓库配置处理。

SDK构建输出另存该job的sdk-output目录并记录库SHA，避免随后build.sh test删除checkout/output后丢失联动物料。Media链接时同时核对源码与SDK产物来源SHA。

4. 持续查询`devbox-status`：`["--task","<task>","--job","<job>","--wait","20","--lines","40"]`。每次等待最多30秒。只有result.status=succeeded且exit_code=0才证明该实际命令成功；start返回0或查询返回0不代表编译通过。failed保留日志并分析；worker消失但无终态为unknown，先查同一job/日志，不盲目重新start。

5. SDK构建完成后，在Media项目准备同一task的Media源码，然后：

```text
--repo media --task <task> --mode media-build --job media-build-<sha8> --sdk-commit <SDK完整SHA>
--repo media --task <task> --mode media-test --job media-test-<sha8> --sdk-commit <SDK完整SHA>
```

联动验证用独立job目录里的go.mod/go.sum和replace覆盖，不改变正式依赖。使用Go1.23、CGO_ENABLED=1和新SDK库目录。Media编译直接`go build -o <jobdir>/byteview_mp_media_platform .`，测试使用仓库`make test ARGS=./business_module/... -count=1`。

**为什么不直接复制Media build.sh执行：** 它会追加HOME下netrc并向`/opt/tiger/...`、`/usr/lib/...`写共享依赖。隔离验证复用已有工具/依赖，使用临时modfile和进程环境，不运行这些全局写入。缺库时按真实错误补齐专用目录依赖，不能暗改系统或生产服务。

worker与日志位于`<task>/.kaki-jobs/<job>/`；本机回执在当前项目state/workflows/devbox。Agent退出或SSH断开不取消后台构建，恢复后查询同一job；超时由worker终止自己的构建进程组。完成报告保留主机、源码SHA、新分支、实际argv、日志路径、退出码、SDK库SHA及联动modfile信息。SDK发布、服务部署与MR合并不是本Skill动作。

脚本按包内容hash安装到本机skill-runtimes，远端worker也按hash保留；开发源文件更新不改正在使用的执行副本。升级时在空闲轮切换profiles并重启加载，不禁用校验，也不删除旧job或日志。远端脚本仅用Python3.5标准库；本机适配器使用Kaki的Python3.12环境。

BPT安装后的rapidjson:1.1.0还声明middleware/sonic-cpp@1.1.0。固定镜像清单包含这第九项：prepare时读取已有远端父BUILD，首次尚未安装允许等待构建下载，SDK成功前必须核验该声明及SHA；不改BPT包。

Media私有Go模块下载停滞时，devbox-go-cache --task <task>仅在Mac下载/读取go.mod/go.sum和replace声明的模块物料，含CI指定init-patcher@v0.0.5；比较远端缓存缺项后传mod/info/zip归档，校验SHA和路径并准备专属file GOPROXY。不会在Mac编译业务，不传凭据，不改全局Go配置。build只覆盖本次环境GONOPROXY=none，Go原有go.sum校验保留。缺失模块版本明确报错，不任意扩大目录或修改正式依赖。

devbox-cancel --task <task> --job <job>仅对经PID/父子关系/进程组核验的本任务发取消信号，未知或复用PID拒绝；新worker终态为cancelled，旧worker可能记录failed/-15。取消后查原job直到终态，再使用新的job ID，源码和产物保留。

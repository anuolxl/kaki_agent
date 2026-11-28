---
name: kaki-code-development
description: 扫描和修改当前Git仓库、修复bug、补测试、更新指定基线并创建开发分支；包含freshness doctor、精确暂存和本地提交。只读review或仅查询测试结果优先选对应Skill。
metadata:
  kaki:
    required_tools: [kaki_git, kaki_command, kaki_read_file, kaki_edit_file, kaki_exec]
    required_commands: [freshness]
---

先根据当前真实任务确定目标仓库和验收条件，读取该仓AGENTS、索引和相关module spec。继承的规则要求freshness doctor时，调用`kaki_command(name="freshness")`；该profile实际运行已配置的bash脚本，不需要一个名为bash的工具。读取warning_count和findings，警告不能当全绿，不自动更新Harness或登录。

用户指定最新release基线时，先`kaki_git(action="fetch",ref="release/YYYY-MM-DD")`，再`create_branch`并提供`base="origin/release/YYYY-MM-DD"`及新branch。保护分支上不能编辑；有跟踪文件改动时保护现场，不stash/reset。分支切换单独调用，返回后按新规则和代码继续；不要在同一批里预先排入写文件。

真实读取后再编辑，补能区分修复前后的测试，遵守仓库的平台与命令约定。Go文件可逐个`kaki_exec(command="gofmt -w path.go")`；media_platform在macOS使用`make test`/`make test ARGS="..."`，不做生产构建。工具失败先根据真实输出调整，不凭印象要求用户开放工具；`kaki_capabilities`报告缺少前提，`kaki_tool_search`加载可发现的工具。

修改后review diff、同步相关SPEC；暂存和提交均列精确文件，拒绝add .。用`kaki_git(action="stage",paths=[...])`，再commit提供同一paths与message。不要带入其它任务的文件。不自动推送、合并或部署；准备MR时使用kaki-prepare-mr。用户明确暂停才停止；缺能力、缺授权和CLI认证失败分别说明，不把ask_user回答当成安装了工具。

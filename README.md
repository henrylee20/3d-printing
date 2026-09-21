# 3D Printing Skills

可复用的 3D 打印相关 agent skills。每个 skill 在 `skills/` 下独立维护。

| Skill | 用途 |
| --- | --- |
| [bambu-print](skills/bambu-print/SKILL.md) | 使用 Bambu Studio CLI 检查悬空风险、切片，并通过已登录云端账号在 P1S 上打印。 |

进入对应 skill 阅读 `SKILL.md`；脚本参数通过 `--help` 查询。仓库只存放技能及源码，打印模型、切片产物、账号配置和缓存保留在本机。

`bambu-print` 的离线测试：

```bash
python skills/bambu-print/scripts/test_workflow.py
```

设置 `BAMBU_TEST_JOB` 为使用当前版本生成的任务目录，可同时验证下发前校验逻辑；测试不会启动打印。

第三方源码的许可证和来源保存在对应 skill 的 `scripts/vendor/` 中。

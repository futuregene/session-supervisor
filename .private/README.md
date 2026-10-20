# 隐私目录（`AGENTS.md` §1 / `docs/privacy-and-state.md` §1）

与人隐私相关、不能提交的东西都放这里；下面只有 README 被跟踪，其余全部忽略。

- **凭据不进任何文件**（这一层永不读、永不复制），凭据只留在系统钥匙串或工具自己的存储里。
- 关于用户的判断（指令风格、口味、长期偏好）放 `.private/profile/`，不写进 `journal/`
  或 `reports/`——那两个目录虽然也已不进 git，但会被推送 Life Journal，且我每轮都会读。

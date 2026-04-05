# MowerResource

每小时检查上游仓库 [ArknigthsGameResource](https://github.com/yuanyan3060/ArknightsGameResource/tree/main/) 是否更新

触发更新后拉取特定的原始资源并构建 Mower 所需的资源

构建完成后自动推送到本仓库并同步至 Mower 项目仓库（Fork）

目前还在使用 Fork 仓库测试中

部分资源需要在本地环境生成

```bash
python generator/auto_get_res_new.py --local-only
```
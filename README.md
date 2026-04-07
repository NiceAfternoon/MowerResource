# MowerResource

每小时检查上游仓库 [ArknigthsGameResource](https://github.com/yuanyan3060/ArknightsGameResource/tree/main/) 是否更新

触发更新后拉取特定的原始资源并构建 Mower 所需的资源

构建完成后自动推送到本仓库并同步至 Mower 项目仓库（Fork）

目前还在使用 Fork 仓库测试中

---

公招更新时部分资源需要在本地环境生成：

在游戏更新后并等待 Actions 自动同步部分上游资源后，先拉取至本地：

```bash
git pull
```

关联上游资源仓库后配置稀疏检出：

```bash
git remote add metadata https://github.com/yuanyan3060/ArknightsGameResource.git
git sparse-checkout init --cone
git sparse-checkout set gamedata/excel item avatar building
```

拉取上游资源：
```bash
git pull metadata main --allow-unrelated-histories
```

再执行资源生成脚本：

```bash
python generator/auto_get_res_new.py --local-only
```
qq-deepseek 配置文件备份
生成时间：2026-10-20

这个目录只放"配置/人格/NapCat 配置"，不含代码、记忆库、日志。
代码已经在 https://github.com/MysteryNGU/qq-deepseek（私有）。

目录内容
--------
config.toml                 主配置（含 API key、owner、通道设置）
config.example.toml         配置模板
persona.md                  当前人格
personas/*.md               其他人格（冷 / 猫娘 / 软 / 颢然）
napcat/*.json               NapCat OneBot / WebUI 配置（账号 3878921941）

来源
----
C:\Users\76328\qq-deepseek\config.toml
C:\Users\76328\qq-deepseek\config.example.toml
C:\Users\76328\qq-deepseek\persona.md
C:\Users\76328\qq-deepseek\personas\
C:\Users\76328\qq-deepseek\napcat-onekey\NapCat.52230.Shell\versions\9.9.33-52230\resources\app\napcat\config\

注意
----
1. config.toml 里有 DeepSeek API key；napcat/webui.json 里可能有 WebUI token/密码。
   这个目录不要传到公开仓库，也不要发给别人。
2. 恢复时把 config.toml / config.example.toml / persona.md 放回项目根目录，
   personas/ 放回项目 personas/，napcat/*.json 放回上面那个 NapCat config 目录。
3. 没有备份：data/（brain.db、evo.db、日志、截图）、.venv、napcat 运行时、代码。
   代码在 git；data 是活的记忆，需要的话单独说。
4. "persona copy.md" 是重复文件，没有放进来。

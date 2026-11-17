# 云端构建手机 APK

日期：2026-11-17。目的：让云端自己构建、发布手机端 APK，不再依赖主人的 Windows 机器出包。

## 结论与边界

- 云端现在能构建：JDK 17 + Android build-tools 35 + platform 35 已装，源码在 `/opt/qq-deepseek/mobile`，签名密钥在 `/root/.android/xiaoyu-signing`。
- 云端构建的包与 Windows 构建的包**签名证书相同**（SHA-256 `fb83594cab08da30e271cd81be8d1b35f581d60e4e39ab695b00662dc48bb6ce`），所以可以覆盖安装，配对与本地数据不变。
- 两次构建**字节不一定相同**（zip 时间戳、压缩顺序差异），判断标准是包名 + 版本 + 证书，不是文件哈希。
- 安装仍需要主人：手机到云端没有入站通道，云端只能把包放到下载地址。

## 一次性准备（已在本机完成）

```bash
# 1) JDK 与解压工具
apt-get update && apt-get install -y openjdk-17-jdk-headless unzip

# 2) Android build-tools 与平台包（约 122 MB，来自 Google 官方仓库）
D=/opt/qq-deepseek/data/android-toolchain
mkdir -p "$D/build-tools-35.0.0" "$D/platforms-android-35"
cd /tmp
curl -sSLO https://dl.google.com/android/repository/build-tools_r35_linux.zip
curl -sSLO https://dl.google.com/android/repository/platform-35_r02.zip
unzip -q -o build-tools_r35_linux.zip -d "$D/build-tools-35.0.0"
unzip -q -o platform-35_r02.zip -d "$D/platforms-android-35"
# 关键文件：android-15/{aapt2,aidl,zipalign,lib/d8.jar,lib/apksigner.jar}、android-35/android.jar

# 3) 源码与依赖（从开发机同步）
#    mobile/  vendor/shizuku/  tools/build-companion-android.py
```

签名密钥从开发机同步，权限必须是 root 独占：

```bash
mkdir -p /root/.android/xiaoyu-signing && chmod 700 /root/.android /root/.android/xiaoyu-signing
# scp xiaoyu.jks password root@<服务器>:/root/.android/xiaoyu-signing/
chmod 600 /root/.android/xiaoyu-signing/*
```

## 日常构建与发布

```bash
bash /opt/qq-deepseek/tools/cloud-build-publish.sh
```

脚本做三件事：跑 `tools/build-companion-android.py` → 把最新 APK 装到 `/var/www/companion/downloads/` → 打印下载地址、文件哈希和证书指纹。

也可以单独构建（不发包）：`cd /opt/qq-deepseek && python3 tools/build-companion-android.py`。

## 验证

```bash
cd /opt/qq-deepseek
APK=$(ls -t data/android-build/output/*.apk | head -1)
java -jar data/android-toolchain/build-tools-35.0.0/android-15/lib/apksigner.jar verify --print-certs "$APK" \
  | grep 'SHA-256 digest'
# 期望：fb83594cab08da30e271cd81be8d1b35f581d60e4e39ab695b00662dc48bb6ce
```

实际结果（2026-11-17）：云端构建 `Xiaoyu-0.4.7.apk`，176939 字节，v2/v3 签名通过，证书指纹与 Windows 构建一致。

## 安全决定（主人已选方案 B）

签名私钥现在同时存在于开发机与云端。**谁能读到这把私钥，谁就能签出能被手机接受的"小鱼更新"**。因此：

- 密钥只在 root 家目录、600 权限；不要放进 `/opt/qq-deepseek`，不要进备份或 Git；
- 收紧 SSH：只允许密钥登录、禁用密码、限制来源 IP；
- 如果服务器曾经泄露、或密钥被复制到别处，应当轮换：用新密钥重新构建并让主人重新安装（换密钥等于换应用签名，必须卸载重装，配对会丢）。

## 给小鱼用

她可以在云端执行构建脚本（她的工作模式能跑 shell）。但要注意两点：

1. 她改的是 `/opt/qq-deepseek/mobile` 下的源码，改完必须走一次构建才会变成 APK；
2. 她能构建、能发布到下载地址，但**装到手机这一步只能由主人完成**（或一台连着手机的机器）。

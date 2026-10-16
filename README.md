# rush_phone

先用专用 Google Chrome 手动登录，再让购买脚本复用这个窗口，持续等待商品开放购买。

## Windows 一键启动

Windows 11 x64 使用打包后的 `RushPhone-Windows-x64.zip`：完整解压，双击 `RushPhone/RushPhone.exe`。需要系统 Google Chrome，无需安装 Python 或配置依赖。`_internal` 文件夹须与 EXE 一起保留。

窗口中只有三个主要入口：**登录、购买 Duo、购买 Pro Max**。先点击登录，在专用 Chrome 中手动完成 Apple 登录和验证并保持窗口打开，再选择机型开始持续监控。默认演练到最终提交前；**勾选“允许提交订单”后会真实下单，付款仍需手动完成。**“停止”或关闭启动器会等待当前网页操作结束，保留 Chrome。

预设规格为 Duo / 512GB / 夜空色，以及 iPhone 18 Pro Max / 512GB / 勃艮第酒红色。两者共用同一登录窗口。配置与登录资料自动保存在 `%LOCALAPPDATA%\RushPhone`，升级不覆盖；窗口可打开该目录，修改 `duo.toml` 或 `pro-max.toml` 后，下次购买重新读取。

Windows 发布包由 [GitHub Actions](https://github.com/stars-bing/rush_phone/actions/workflows/windows.yml) 在 Windows 上构建，成功后下载 `RushPhone-Windows-x64` 产物，详见 [打包说明](packaging/windows/README.md)。离线测试与自检不能代替 Windows 实机登录和真实 Apple 结账验证。

## Mac 原有命令行方案

Mac 继续使用下面三条命令：先登录，再选择 Duo 或 Pro Max。**两条购买命令带有 `--submit-order`，会真正提交订单；付款由你在原窗口手动完成。**

## 1. 登录

在项目目录、已安装依赖的 Python 环境中运行：

```bash
python -m rush_phone chrome-login --config config.toml
```

在打开的专用 Chrome 中完成 Apple 登录和双重认证，登录成功后保持窗口打开。

统一使用已确认可以正常登录的 `chrome-login` 路径。“Chrome 已就绪”表示窗口已打开，仍需你在页面上确认登录成功。购买命令直接连接这个窗口；连接记录无效或窗口已关闭时会停止，不另开浏览器代替。

Duo 和 Pro Max 的本地配置目前共用同一个账号资料目录，登录一次后即可选择其中一种机型继续。

## 2. 购买 iPhone Duo

```bash
python -m rush_phone run --config config.toml --submit-order
```

当前本地配置：**iPhone Duo / 512GB / 夜空色**。

## 3. 购买 iPhone Pro Max

```bash
python -m rush_phone run --config config.test-pro-max.local.toml --submit-order
```

当前本地配置：**iPhone 18 Pro Max / 512GB / 勃艮第酒红色**。

两个购买命令选择一个执行。同一账号同时只允许一个购买或测试脚本操作专用窗口，避免同时加购和提交。

## 开售前与开售后如何执行

1. 连接 `chrome-login` 打开的原 Chrome、原浏览器会话和已有标签页，核对登录状态。
2. 打开商品页，选择配置中的机型、颜色、容量等规格。默认每约 1 秒检查购买按钮状态；Duo 对应的是“继续”按钮。
3. 按钮不可点击时持续等待。为处理页面不会自行更新的情况，默认每 5 秒在同一标签页刷新一次，并重新选择相同规格；不会重新启动浏览器或重新登录。实际检查周期还包含页面加载和操作耗时。
4. 按钮可点击且配送查询成功后，核对购物袋：空袋时加购一次；已有完全匹配、数量为 1 的商品时跳过加购；商品或数量不符时停止，交给你人工整理。
5. 进入结账。若页面需要人工登录、双重认证、确认配送、收货地址或付款方式，请在当前窗口处理。脚本不读取配置密码自动登录。
6. 最终提交按钮可见、可用且唯一时，只点击一次，并等待配置中的订单成功或待支付状态。
7. 确认结果后结束脚本连接，保留原 Chrome，供你核对订单和手动付款。

**`run` 会持续等待开放购买，直到可购买、发生错误、窗口关闭或你按 Ctrl+C。** 开售等待不受人工验证的 300 秒时限限制；`challenge_timeout_seconds` 只控制人工登录、验证或结账中间页面的等待。

加购、结账和最终提交不自动重试。遇到 HTTP 541 等请求错误时停止并保留窗口。如果已尝试最终提交却无法确认结果，程序以退出码 3 停止，提示你核对订单；不要直接再次运行购买命令。

## 测试与配置

- 要持续等开售并演练到最终提交前，去掉购买命令中的 `--submit-order`。`session-test` 仍是单次测试，不持续等开售，也不提交订单。
- Mac 命令行需要 Python 3.11 或更新版本、系统 Google Chrome，以及已安装的项目依赖。命令中的 `python` 应指向项目虚拟环境；Windows 一键版直接使用窗口按钮。
- Duo 使用 `config.toml`；Pro Max 使用 `config.test-pro-max.local.toml`。修改规格时编辑对应文件的 `[variables]`。
- 两份配置的启用账号与 `profile_dir` 必须一致，才能复用同一个登录窗口。换电脑时使用本机路径，推荐相对路径 `.profiles/apple-cn-account-1`。一次购买只选择一个账号。
- 按钮检查间隔由 `[run]` 的 `poll_interval_ms` 和 `poll_jitter_ms` 控制；同一标签页刷新间隔由 `stock_refresh_interval_ms` 控制，默认 5000，最低 1000，单位均为毫秒。
- 收货地址在 Apple 账户或结账页面中填写。购买流程按 `flow.steps` 执行；Apple 页面若仍要求确认配送、地址或付款方式，需要人工继续，等待时限由 `challenge_timeout_seconds` 控制。
- 最终提交后的 `flow.steps` 只能包含等待步骤，至少有一个订单成功或待支付的正向确认条件。若这些条件在提交前就全部满足，程序会要求校准选择器并拒绝提交，避免误报成功。
- 专用 Chrome 关闭后，需要重新运行登录命令；Apple 可能要求重新认证。
- 购买期间保持专用 Chrome 窗口可见、本机不休眠，避免后台页面暂停更新或控件等待超时。
- 连接浏览器前应取消 `DEBUG` / `PWDEBUG`；项目的 `--verbose` 可以使用。

## 遇到问题

- **没有专用 Chrome 或连接记录失效**：重新执行登录命令，完成登录并保持窗口打开。
- **尚未确认登录或需要验证**：在专用窗口完成人工登录或验证；购买中的验证会暂停等待，不自动填写密码。
- **HTTP 541、配送查询错误或结账失败**：查看保留的窗口，先确认页面和订单状态，不连续重复执行购买。
- **购物袋内容不匹配**：人工整理为目标机型、颜色、容量，数量为 1，或者清空后重新运行。
- **结账按钮等待超时**：实际页面可能需要人工确认中间步骤，或 `flow.steps` 的选择器需要按页面校准；开售后的真实结账流程仍需核对。

日志位于 `logs/rush_phone.log`，关键页面截图位于 `artifacts/`。`.profiles/` 包含登录资料和浏览器连接记录，请勿提交或分享。

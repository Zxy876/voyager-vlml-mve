# MVEBridge 编译成 DLL（备选）

`MVEBridge.cs` 依赖 ScriptHookVDotNet3 + System.Web.Extensions。
如果游戏目录的 `scripts/` 不支持 .cs 源码加载，再按这里编译。

## 用 .NET Framework 自带的 csc（Windows 最省事）

在开发者命令提示符（或直接用完整路径 `C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe`）：

```
csc /target:library /out:MVEBridge.dll ^
  /reference:ScriptHookVDotNet3.dll ^
  /reference:System.Web.Extensions.dll ^
  /reference:System.dll /reference:System.Core.dll ^
  MVEBridge.cs
```

其中 `ScriptHookVDotNet3.dll` 来自 SHVDN v3 的 release 包。
编译出的 `MVEBridge.dll` 放进游戏根目录的 `scripts/`。

## 验证加载

- 游戏根目录 `ScriptHookVDotNet.log`：应有 `Loading script MVEBridge` / `Compiled`
- 进游戏剧情模式，右上角出现「MVEBridge 启动」式的通知
- 端口检查：`netstat -ano | findstr 9777` 应有 LISTENING

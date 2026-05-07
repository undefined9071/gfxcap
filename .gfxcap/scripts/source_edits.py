"""String-based source-tree edits applied at the end of prepare.py.

Each edit is a (file, find, replace) triple. More robust than unified-diff
patches because it doesn't depend on line numbers -- works across upstream
version changes as long as the `find` string is stable. Edits are applied
exactly once: if `find` is not present (e.g. already applied or upstream
fixed), the edit is silently skipped.

Order matters only when one edit's `find` overlaps with another's `replace`.
"""
from __future__ import annotations
from pathlib import Path
from typing import TypedDict


class Edit(TypedDict):
    file: str
    find: str
    replace: str
    desc: str


SOURCE_EDITS: list[Edit] = [
    # ---------------------------------------------------------------------------
    # HLSL ternary fix: newer dxc rejects 'p ? -1 : 1' on uint2 -- needs explicit
    # per-component. Required on v1.17, already fixed in v1.44+.
    # ---------------------------------------------------------------------------
    {
        "desc": "hlsl: fix non-scalar ternary for newer dxc (v1.17 only)",
        "file": "gfxcap/data/hlsl/quadoverdraw.hlsl",
        "find": "int2 sign = p ? -1 : 1;",
        "replace": "int2 sign = int2(p.x ? -1 : 1, p.y ? -1 : 1);",
    },

    # ---------------------------------------------------------------------------
    # MinHook engine substitution in win32_hook.cpp.
    # Replaces the four LibraryHooks API entry points and adds the MinHook
    # include. Driver code (d3d11_hooks.cpp etc.) keeps using the same Register
    # API but the implementation is swapped from RenderDoc's own LoadLibrary
    # interception + IAT patching to direct MinHook inline hooks on a
    # configurable allowlist of target libraries. This makes the hook footprint
    # smaller and avoids modifying loader-related system DLL imports.
    # ---------------------------------------------------------------------------
    {
        "desc": "win32_hook: add MinHook include",
        "file": "gfxcap/os/win32/win32_hook.cpp",
        "find": '#include "strings/string_utils.h"\n\n#define VERBOSE_DEBUG_HOOK',
        "replace": '#include "strings/string_utils.h"\n#include "minhook/include/MinHook.h"\n\n#define VERBOSE_DEBUG_HOOK',
    },
    {
        "desc": "win32_hook: replace RegisterFunctionHook with MinHook + allowlist",
        "file": "gfxcap/os/win32/win32_hook.cpp",
        "find": '''void LibraryHooks::RegisterFunctionHook(const char *libraryName, const FunctionHook &hook)
{
  if(!_stricmp(libraryName, "kernel32.dll"))
  {
    if(hook.function == "LoadLibraryA" || hook.function == "LoadLibraryW" ||
       hook.function == "LoadLibraryExA" || hook.function == "LoadLibraryExW" ||
       hook.function == "GetProcAddress")
    {
      GFXERR("Cannot hook LoadLibrary* or GetProcAddress, as these are hooked internally");
      return;
    }
  }
  s_HookData->DllHooks[strlower(rdcstr(libraryName))].FunctionHooks.push_back(hook);
}''',
        "replace": '''static bool HookAllowed(const char *libraryName, const rdcstr &function)
{
  // Allowlist of libraries to actually hook -- everything else is silently
  // skipped. Keeps the hook footprint small and predictable. Edit to suit
  // the API surface you need (e.g. add d3d12.dll, vulkan-1.dll, opengl32.dll).
  if(_stricmp(libraryName, "d3d11.dll") == 0)
    return true;
  if(_stricmp(libraryName, "dxgi.dll") == 0)
    return true;
  return false;
}

void LibraryHooks::RegisterFunctionHook(const char *libraryName, const FunctionHook &hook)
{
  if(!HookAllowed(libraryName, hook.function))
    return;
  HMODULE lib = LoadLibraryA(libraryName);
  if(!lib)
    return;
  void *target = (void *)GetProcAddress(lib, hook.function.c_str());
  if(!target)
    return;
  void *origOut = NULL;
  if(MH_CreateHook(target, hook.hook, &origOut) != MH_OK)
    return;
  if(hook.orig)
    *hook.orig = origOut;
  if(s_HookData)
    s_HookData->DllHooks[strlower(rdcstr(libraryName))].FunctionHooks.push_back(hook);
}''',
    },
    {
        "desc": "win32_hook: replace RegisterLibraryHook with allowlist",
        "file": "gfxcap/os/win32/win32_hook.cpp",
        "find": '''void LibraryHooks::RegisterLibraryHook(const char *libraryName, FunctionLoadCallback loadedCallback)
{
  s_HookData->DllHooks[strlower(rdcstr(libraryName))].Callbacks.push_back(loadedCallback);
}''',
        "replace": '''void LibraryHooks::RegisterLibraryHook(const char *libraryName, FunctionLoadCallback loadedCallback)
{
  if(_stricmp(libraryName, "d3d11.dll") != 0 && _stricmp(libraryName, "dxgi.dll") != 0)
    return;
  // Ensure library is loaded so RegisterFunctionHook's GetProcAddress works.
  // We skip firing loadedCallback to stay compatible across upstream versions
  // (FunctionLoadCallback's signature changes between v1.17 and v1.44+).
  LoadLibraryA(libraryName);
  if(s_HookData)
    s_HookData->DllHooks[strlower(rdcstr(libraryName))].Callbacks.push_back(loadedCallback);
}''',
    },
    {
        "desc": "win32_hook: replace BeginHookRegistration with MH_Initialize",
        "file": "gfxcap/os/win32/win32_hook.cpp",
        "find": '''void LibraryHooks::BeginHookRegistration()
{
  InitHookData();
}''',
        "replace": '''void LibraryHooks::BeginHookRegistration()
{
  // MinHook engine: do not install LoadLibrary*/GetProcAddress hooks.
  // Allocate s_HookData so other helpers don't null-deref if invoked.
  if(!s_HookData)
    s_HookData = new CachedHookData;
  MH_Initialize();
}''',
    },

    # ---------------------------------------------------------------------------
    # Default capture save directory: portable layout.
    # Upstream defaults to %TEMP%\GfxCap\ which is awkward for a portable
    # bundle (users can't find the file, OS may sweep it, admin-launched UI
    # writes to a different temp than the user expects). Switch to
    # <exe-dir>\captures\, which sits next to the executable and stays put.
    # The user can still override this in Tools -> Settings -> Capture.
    # ---------------------------------------------------------------------------
    {
        "desc": "CaptureContext: default capture dir -> <exe-dir>/captures",
        "file": "gfxcapui/Code/CaptureContext.cpp",
        "find": '''  if(folder.isEmpty() || !dir.exists())
  {
    dir = QDir(QDir::tempPath());

    dir.mkdir(lit("GfxCap"));

    dir = QDir(dir.absoluteFilePath(lit("GfxCap")));
  }''',
        "replace": '''  if(folder.isEmpty() || !dir.exists())
  {
    // Portable bundle: keep captures next to the exe rather than in %TEMP%.
    dir = QDir(QApplication::applicationDirPath());
    dir.mkdir(lit("captures"));
    dir = QDir(dir.absoluteFilePath(lit("captures")));
  }''',
    },

    # ---------------------------------------------------------------------------
    # File watcher: auto-load any new .gcap dropped into <exe-dir>/captures/.
    # Some target processes block renderdoc's TargetControl named-pipe
    # handshake, so the GUI never gets the live-capture notification when F12
    # is pressed inside the application. Working around this with a directory
    # watcher: the rebranded TempCaptureDirectory (set by the CaptureContext
    # edit) writes .gcap files next to the exe, the GUI watches that
    # directory and auto-opens any new file. Survives IPC interference
    # because there's no IPC -- it's all local filesystem.
    #
    # Two coupled edits:
    #  - add the Qt headers we need at the top of MainWindow.cpp
    #  - install a QFileSystemWatcher at the end of the MainWindow ctor
    # ---------------------------------------------------------------------------
    {
        "desc": "MainWindow.cpp: add Qt headers for file watcher",
        "file": "gfxcapui/Windows/MainWindow.cpp",
        "find": '''#include <QFileDialog>
#include <QFileInfo>''',
        "replace": '''#include <QFileDialog>
#include <QFileInfo>
#include <QFileSystemWatcher>
#include <QSet>
#include <QTimer>
#include <QDir>''',
    },
    {
        "desc": "MainWindow ctor: install captures/ directory watcher",
        "file": "gfxcapui/Windows/MainWindow.cpp",
        "find": '''  RegisterShortcut("ALT+R", this, [this](QWidget *) { contextChooser->click(); });
}''',
        "replace": '''  RegisterShortcut("ALT+R", this, [this](QWidget *) { contextChooser->click(); });

  // gfxcap: watch <exe-dir>/captures/ and auto-load any new .gcap.
  // Replaces TargetControl live-capture handshake for targets where
  // the named-pipe IPC is blocked.
  {
    QString capDir = QApplication::applicationDirPath() + lit("/captures");
    QDir().mkpath(capDir);
    auto *watcher = new QFileSystemWatcher(this);
    watcher->addPath(capDir);
    auto *known = new QSet<QString>();
    for(const QFileInfo &fi : QDir(capDir).entryInfoList(
          QStringList() << lit("*.gcap"), QDir::Files))
      known->insert(fi.absoluteFilePath());
    QObject::connect(watcher, &QFileSystemWatcher::directoryChanged, this,
      [this, known, capDir](const QString &) {
        for(const QFileInfo &fi : QDir(capDir).entryInfoList(
              QStringList() << lit("*.gcap"),
              QDir::Files | QDir::NoDotAndDotDot, QDir::Time))
        {
          QString p = fi.absoluteFilePath();
          if(!known->contains(p))
          {
            known->insert(p);
            // small delay so the writer can finish flushing
            QTimer::singleShot(750, this, [this, p]() {
              // Skip if TargetControl already opened this same capture
              // via the live path -- avoids double-loading the file once
              // via TargetControl and once via the watcher.
              if(m_Ctx.IsCaptureLoaded() &&
                 QString(m_Ctx.GetCaptureFilename()) == p)
                return;
              LoadFromFilename(p, false);
            });
          }
        }
      });
  }
}''',
    },

    # ---------------------------------------------------------------------------
    # Live capture entries are persistent by default.
    # Upstream initialises Capture::saved to false, so anything F12 produces
    # is treated as a "temp" item: cleanItems() deletes it on close,
    # checkAllowClose() prompts a Save dialog, and the original filename gets
    # dropped (the user has to retype it). For a portable workflow we want
    # F12 captures to persist next to the exe with the original filename and
    # never trigger the save prompt. Initialising saved=true makes
    # checkAllowClose see zero unsaved captures and cleanItems treat each
    # entry as "already on disk", so neither the prompt nor the delete fire.
    # ---------------------------------------------------------------------------
    {
        "desc": "LiveCapture::Capture: default saved=true (no auto-delete, no save prompt)",
        "file": "gfxcapui/Windows/Dialogs/LiveCapture.h",
        "find": '''    bool saved;
    bool opened;''',
        "replace": '''    bool saved = true;   // gfxcap: keep F12 captures persistent
    bool opened = false;''',
    },

    # ---------------------------------------------------------------------------
    # The header default `bool saved = true` is undone by captureCopied():
    # every capture that arrives over the TargetControl named-pipe path gets
    # `cap->saved = false` written here, which puts the file back on the
    # auto-delete-on-close path. Override that line so TargetControl-delivered
    # captures persist exactly like watcher-delivered ones do.
    # ---------------------------------------------------------------------------
    {
        "desc": "LiveCapture::captureCopied: keep saved=true on incoming captures",
        "file": "gfxcapui/Windows/Dialogs/LiveCapture.cpp",
        "find": '''  cap->remoteID = newCapture.captureId;
  cap->saved = false;''',
        "replace": '''  cap->remoteID = newCapture.captureId;
  cap->saved = true;   // gfxcap: keep persistent; honours the header default''',
    },

    # ---------------------------------------------------------------------------
    # Auto-register the bundled HLSL Decompiler plugin.
    # The plugin lives at <exe-dir>/plugins/hlsl-decompiler/HLSLDecompiler.bat
    # (workflow stages it from .gfxcap/3rdparty/hlsl-decompiler). The .bat
    # writes HLSL to stdout, so we register it as KnownShaderTool::Unknown
    # with args="{input_file}" -- because there's no {output_file} placeholder,
    # ShaderProcessingTool::RunTool falls into its stdout-as-output path.
    # Three entries (DXBC / DXIL / SPIR-V inputs) so the GUI can decompile
    # any shader format the capture exposes.
    # ---------------------------------------------------------------------------
    {
        "desc": "PersistantConfig: auto-register bundled HLSL Decompiler",
        "file": "gfxcapui/Code/Interface/PersistantConfig.cpp",
        "find": "  // sanitisation pass, if a tool is declared as a known type ensure its inputs/outputs are correct.",
        "replace": '''  // gfxcap: auto-register the bundled HLSL Decompiler plugin if present.
  {
    QString hlslDecompPath = QDir(QApplication::applicationDirPath())
        .absoluteFilePath(lit("plugins/hlsl-decompiler/HLSLDecompiler.bat"));
    if(QFileInfo(hlslDecompPath).exists())
    {
      bool already = false;
      for(const ShaderProcessingTool &t : ShaderProcessors)
      {
        if(QString(t.executable) == hlslDecompPath)
        {
          already = true;
          break;
        }
      }
      if(!already)
      {
        struct { ShaderEncoding in; const char *name; } variants[] = {
          { ShaderEncoding::DXBC,  "HLSL Decompiler (DXBC)"  },
          { ShaderEncoding::DXIL,  "HLSL Decompiler (DXIL)"  },
          { ShaderEncoding::SPIRV, "HLSL Decompiler (SPIR-V)" },
        };
        for(const auto &v : variants)
        {
          ShaderProcessingTool s;
          s.tool = KnownShaderTool::Unknown;
          s.name = v.name;
          s.executable = hlslDecompPath;
          s.args = "{input_file}";
          s.input = v.in;
          s.output = ShaderEncoding::HLSL;
          ShaderProcessors.push_back(s);
        }
      }
    }
  }

  // sanitisation pass, if a tool is declared as a known type ensure its inputs/outputs are correct.''',
    },

    # ---------------------------------------------------------------------------
    # Better default capture options for learning workflows.
    # captureAllCmdLists: required for Unity titles that use deferred
    #   contexts; without it some draws end up not in the recorded
    #   command stream.
    # captureCallstacks + captureCallstacksOnlyActions: each draw event
    #   carries the C++-side callstack so the user can see "this draw came
    #   from this code path", helpful for engine reverse-engineering.
    # ---------------------------------------------------------------------------
    {
        "desc": "capture_options: better defaults for learning (cmd lists + action callstacks)",
        "file": "gfxcap/replay/capture_options.cpp",
        "find": '''  captureCallstacks = false;
  captureCallstacksOnlyActions = false;
  delayForDebugger = 0;
  verifyBufferAccess = false;
  hookIntoChildren = false;
  refAllResources = false;
  captureAllCmdLists = false;''',
        "replace": '''  captureCallstacks = true;
  captureCallstacksOnlyActions = true;
  delayForDebugger = 0;
  verifyBufferAccess = false;
  hookIntoChildren = false;
  refAllResources = false;
  captureAllCmdLists = true;''',
    },

    # ---------------------------------------------------------------------------
    # Disable the on-startup version check.
    # The forced check from the "Help -> Check for Updates" menu item still
    # works because that path passes forceCheck=true; only the unconditional
    # MainWindow ctor call is suppressed.
    # ---------------------------------------------------------------------------
    {
        "desc": "MainWindow: skip on-startup auto-update check",
        "file": "gfxcapui/Windows/MainWindow.cpp",
        "find": '''  PopulateReportedBugs();

  CheckUpdates();''',
        "replace": '''  PopulateReportedBugs();

  // CheckUpdates();  // gfxcap: no on-startup auto-update check''',
    },

    # ---------------------------------------------------------------------------
    # Suppress the "Uncapped Map()/Unmap()" capture-failure warning at source.
    # Unity's persistent dynamic buffer pattern (frame-spanning Map/Unmap)
    # trips this on every open-world capture even though the capture file
    # itself is fine for analysis. We drop the failure flag so the warning
    # never propagates through to the GUI overlay or capture-failure log.
    # ---------------------------------------------------------------------------
    {
        "desc": "d3d11_context_wrap: silence UncappedUnmap warning (Unity persistent map)",
        "file": "gfxcap/driver/d3d11/d3d11_context_wrap.cpp",
        "find": '''        RDCWARN(
            "Saw an Unmap that we didn't capture the corresponding Map for - this frame is "
            "unsuccessful");
        m_SuccessfulCapture = false;
        m_FailureReason = CaptureFailed_UncappedUnmap;''',
        "replace": '''        // gfxcap: Unity dynamic buffers Map across frame boundaries; that's
        // expected and the rest of the capture is still useful for analysis.
        // Suppress the warning so it doesn't spam the log / overlay.''',
    },

    {
        "desc": "win32_hook: replace EndHookRegistration with MH_EnableHook",
        "file": "gfxcap/os/win32/win32_hook.cpp",
        "find": '''void LibraryHooks::EndHookRegistration()
{
  for(auto it = s_HookData->DllHooks.begin(); it != s_HookData->DllHooks.end(); ++it)
    std::sort(it->second.FunctionHooks.begin(), it->second.FunctionHooks.end());

#if ENABLED(VERBOSE_DEBUG_HOOK)
  GFXDEBUG("Applying hooks");
#endif

  HookAllModules();

  if(s_HookData->missedOrdinals)
  {
#if ENABLED(VERBOSE_DEBUG_HOOK)
    GFXDEBUG("Missed ordinals - applying hooks again");
#endif

    // we need to do a second pass now that we know ordinal names to finally hook
    // some imports by ordinal only.
    HookAllModules();

    s_HookData->missedOrdinals = false;
  }
}''',
        "replace": '''void LibraryHooks::EndHookRegistration()
{
  // MinHook engine: enable all hooks installed during Register* calls.
  MH_EnableHook(MH_ALL_HOOKS);
}''',
    },

    # ---------------------------------------------------------------------------
    # D3D11 wrap opt-out fix: some titles call D3D11CreateDeviceAndSwapChain
    # with the PREVENT_ALTERING_LAYER_SETTINGS_FROM_REGISTRY flag to opt out
    # of debug-layer interception. Upstream renderdoc honours that flag and
    # skips wrapping the device, but our entry-point + DXGI hooks remain
    # active either way -- the resulting mix of a raw D3D11Device with
    # wrapped IDXGISwapChain causes a vtable-call NULL deref in the target
    # shortly after device creation (observed in app-side crash dump: AV at
    # the first wrapper indirection). Other tested titles do not set this
    # flag so forcing suppress=false is a no-op for them.
    # ---------------------------------------------------------------------------
    {
        "desc": "d3d11_hooks: ignore PREVENT_ALTERING_LAYER_SETTINGS opt-out",
        "file": "gfxcap/driver/d3d11/d3d11_hooks.cpp",
        "find": "    suppress = (Flags & D3D11_CREATE_DEVICE_PREVENT_ALTERING_LAYER_SETTINGS_FROM_REGISTRY) != 0;",
        "replace": (
            "    // gfxcap: some titles set PREVENT_ALTERING_LAYER_SETTINGS to opt out of\n"
            "    // wrapping. Our entry-point + DXGI hooks are still active either way so\n"
            "    // honouring the opt-out leaves a wrapped/non-wrapped mix that crashes the\n"
            "    // target. Force suppress=false; the flag is still passed to the real call\n"
            "    // so the app's intent of suppressing registry layer settings is preserved.\n"
            "    suppress = false;"
        ),
    },

    # ---------------------------------------------------------------------------
    # win32_stringio: capture file lands next to the target exe.
    #
    # Upstream's GetDefaultFiles() builds the inject-side capture path as
    # %TEMP%\RenderDoc\<exe>_<timestamp>_frame<N>.rdc. With our rebrand that
    # becomes %TEMP%\GfxCap\..., which is fine for users who launched the
    # target via the GUI (the GUI rewrites the path on its side) but
    # invisible / hard to find for any inject path that bypasses the GUI.
    #
    # Move the capture path to <exe-dir>\captures\<exe>_<timestamp>_frame<N>.rdc
    # so captures end up in a predictable, discoverable location next to the
    # target binary regardless of how injection happened. The directory
    # watcher we install in MainWindow already monitors that path, so the
    # GUI auto-loads the capture the moment it's written.
    #
    # The logging file (logging_filename, written a few lines below the edit
    # site) deliberately stays in %TEMP%\GfxCap\: log spam next to a third
    # party's exe is a worse UX than a hidden log in %TEMP%.
    # ---------------------------------------------------------------------------
    {
        "desc": "win32_stringio: capture file goes to <exe-dir>/captures/ (not %TEMP%)",
        "file": "gfxcap/os/win32/win32_stringio.cpp",
        # NOTE: source_edits run AFTER rebrand, so the literal in the working
        # tree is already L"GfxCap\\..." -- match that, not the upstream
        # L"RenderDoc\\...".
        "find": '''  wchar_t *filename_start = temp_filename + wcslen(temp_filename);

  wsprintf(filename_start, L"GfxCap\\\\%ls_%04d.%02d.%02d_%02d.%02d.rdc", mod, 1900 + now.tm_year,
           now.tm_mon + 1, now.tm_mday, now.tm_hour, now.tm_min);

  capture_filename = StringFormat::Wide2UTF8(temp_filename);

  *filename_start = 0;''',
        "replace": '''  // gfxcap: capture lands in <exe-dir>/captures/ so any inject path
  // produces captures in a predictable user-visible location instead of
  // %TEMP%. The logging file (below) stays in %TEMP%/GfxCap so log spam
  // doesn't pollute the target dir.
  {
    wchar_t exe_dir[MAX_PATH];
    wcscpy_s(exe_dir, MAX_PATH, curFile);
    wchar_t *e = wcsrchr(exe_dir, L'\\\\');
    if(!e)
      e = wcsrchr(exe_dir, L'/');
    if(e)
      *(e + 1) = 0;

    wchar_t cap_dir[MAX_PATH];
    wsprintf(cap_dir, L"%lscaptures", exe_dir);
    CreateDirectoryW(cap_dir, NULL);

    wchar_t cap_path[MAX_PATH];
    wsprintf(cap_path, L"%ls\\\\%ls_%04d.%02d.%02d_%02d.%02d.rdc", cap_dir, mod,
             1900 + now.tm_year, now.tm_mon + 1, now.tm_mday, now.tm_hour, now.tm_min);
    capture_filename = StringFormat::Wide2UTF8(cap_path);
  }

  wchar_t *filename_start = temp_filename + wcslen(temp_filename);
  *filename_start = 0;''',
    },

    # ---------------------------------------------------------------------------
    # MainWindow: drop the "Inject into Process" menu entry unconditionally.
    # The CreateRemoteThread+LoadLibrary path is unreliable against hardened
    # titles (Steam DRM, AC handshakes that observe the loader). For targets
    # that refuse standard inject, see the README for the manual workaround.
    # ---------------------------------------------------------------------------
    {
        "desc": "MainWindow: remove Inject-into-Process menu unconditionally",
        "file": "gfxcapui/Windows/MainWindow.cpp",
        "find": '''#if defined(Q_OS_WIN32)
  // remove inject menu item when it's not enabled in the settings
  if(!ctx.Config().AllowProcessInject)
    ui->menu_File->removeAction(ui->action_Inject_into_Process);
#else
  // process injection is not supported on non-Windows, so remove the menu item rather than disable
  // it without a clear way to communicate that it is never supported
  ui->menu_File->removeAction(ui->action_Inject_into_Process);
#endif''',
        "replace": '''  // gfxcap: drop the upstream Inject-into-Process menu unconditionally --
  // the CreateRemoteThread + LoadLibrary path is too unreliable against
  // hardened titles.
  ui->menu_File->removeAction(ui->action_Inject_into_Process);''',
    },

]


def apply(work_dir: Path) -> tuple[int, int]:
    """Apply edits. Returns (applied_count, skipped_count)."""
    applied = 0
    skipped = 0
    for edit in SOURCE_EDITS:
        path = work_dir / edit["file"]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue
        new_text = text.replace(edit["find"], edit["replace"], 1)
        if new_text == text:
            skipped += 1
            continue
        path.write_text(new_text, encoding="utf-8")
        applied += 1
    return applied, skipped

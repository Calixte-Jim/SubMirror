; =============================================================================
; SubMirror v0.1.18 - NSIS 3.x installer script
;
; Build order (must be run from the repository root):
;   1)  pyinstaller --noconfirm --clean --windowed --name SubMirror main.py
;   2)  makensis build_installer.nsi
;       (or: makensis /DDIST_DIR=dist\SubMirror /DOUT_FILE=SubMirror-setup.exe build_installer.nsi)
;
; Notes:
;   * Per-user install into %LOCALAPPDATA%\SubMirror -> no UAC prompt, no HKLM writes.
;     The application keeps its config in %APPDATA%\SubMirror\config.json, so
;     registry + config stay in the same (HKCU) hive.
;   * The payload is the PyInstaller ONEDIR output (SubMirror.exe next to
;     _internal\). A onefile build is intentionally NOT used: it would extract
;     hundreds of MB into %TEMP% on every launch.
;   * "Is the app running?" is answered with the application's own single
;     instance mutex (Local\SubMirror.SingleInstance.v1, see src/core/context.py)
;     via the bundled System plugin - no third-party NSIS plugin required.
;   * The installer is unsigned in v0.1. Windows SmartScreen may warn on first
;     run; code signing is a release step, not a source change.
; =============================================================================

Unicode true
ManifestDPIAware true
CRCCheck on
SetCompressor /SOLID lzma
SetCompressorDictSize 64

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WinVer.nsh"
!include "FileFunc.nsh"

; -----------------------------------------------------------------------------
; Project identity
; -----------------------------------------------------------------------------
!ifndef APP_VERSION
  !define APP_VERSION "0.1.19"
!endif

!define APP_NAME        "SubMirror"
!define APP_EXE         "SubMirror.exe"
!define APP_VERSION_4   "${APP_VERSION}.0"          ; x.x.x.x for VIProductVersion
!define APP_PUBLISHER   "SubMirror Project"         ; <-- replace before release
!define APP_URL         "https://example.invalid/SubMirror"   ; <-- replace before release
!define APP_COPYRIGHT   "MIT License"

!define INSTALL_KEY     "Software\SubMirror"
!define UNINSTALL_KEY   "Software\Microsoft\Windows\CurrentVersion\Uninstall\SubMirror"

; Must match src/core/context.py::MUTEX_NAME and ACTIVATE_MESSAGE.
!define MUTEX_NAME      "Local\SubMirror.SingleInstance.v1"
!define ACTIVATE_MSG    32769                        ; WM_APP (0x8000) + 1

; -----------------------------------------------------------------------------
; Build inputs (override with /D... on the makensis command line)
; -----------------------------------------------------------------------------
!ifndef SOURCE_DIR
  !define SOURCE_DIR "."
!endif
!ifndef DIST_DIR
  !define DIST_DIR "${SOURCE_DIR}\dist\SubMirror"
!endif
!ifndef OUT_FILE
  !define OUT_FILE "SubMirror-${APP_VERSION}-setup.exe"
!endif

; Optional icon; only used when defined, e.g. /DAPP_ICON=assets\submirror.ico
!ifdef APP_ICON
  !define MUI_ICON   "${APP_ICON}"
  !define MUI_UNICON "${APP_ICON}"
!endif

; -----------------------------------------------------------------------------
; Installer basics
; -----------------------------------------------------------------------------
Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUT_FILE}"
InstallDir "$LOCALAPPDATA\SubMirror"
InstallDirRegKey HKCU "${INSTALL_KEY}" "InstallLocation"   ; upgrade in place
RequestExecutionLevel user
BrandingText "${APP_NAME} ${APP_VERSION}"

VIProductVersion "${APP_VERSION_4}"
VIAddVersionKey /LANG=2052 "ProductName"     "${APP_NAME}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey /LANG=2052 "FileVersion"     "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion"  "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey /LANG=2052 "LegalCopyright"  "${APP_COPYRIGHT}"
VIAddVersionKey /LANG=1033 "ProductName"     "${APP_NAME}"
VIAddVersionKey /LANG=1033 "FileDescription" "${APP_NAME} Setup"
VIAddVersionKey /LANG=1033 "FileVersion"     "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion"  "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey /LANG=1033 "LegalCopyright"  "${APP_COPYRIGHT}"

Var StartMenuFolder

; -----------------------------------------------------------------------------
; Modern UI 2 configuration
; -----------------------------------------------------------------------------
!define MUI_ABORTWARNING
!define MUI_LANGDLL_ALLLANGUAGES

!define MUI_WELCOMEPAGE_TITLE "$(SM_WELCOME_TITLE)"
!define MUI_WELCOMEPAGE_TEXT  "$(SM_WELCOME_TEXT)"

!define MUI_STARTMENUPAGE_REGISTRY_ROOT      "HKCU"
!define MUI_STARTMENUPAGE_REGISTRY_KEY       "${INSTALL_KEY}"
!define MUI_STARTMENUPAGE_REGISTRY_VALUENAME "StartMenuFolder"
!define MUI_STARTMENUPAGE_DEFAULTFOLDER      "${APP_NAME}"

!define MUI_FINISHPAGE_RUN           "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT      "$(SM_RUN_TEXT)"
!define MUI_FINISHPAGE_LINK          "$(SM_FINISH_LINK)"
!define MUI_FINISHPAGE_LINK_LOCATION "${APP_URL}"
!define MUI_FINISHPAGE_NOREBOOTSUPPORT

; -----------------------------------------------------------------------------
; Pages
; -----------------------------------------------------------------------------
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${SOURCE_DIR}\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_STARTMENU Application $StartMenuFolder
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

; Language picker (persisted under HKCU\Software\SubMirror\Installer Language).
; MUI_LANGDLL_DISPLAY expands to commands that NSIS only accepts inside a
; Function or Section, so the macro is inserted at the top of Function .onInit
; below instead of at script level.

; -----------------------------------------------------------------------------
; Localised strings (every user-visible custom text needs both languages)
; -----------------------------------------------------------------------------
LangString SM_DISPLAYNAME      ${LANG_SIMPCHINESE} "SubMirror 实时字幕翻译"
LangString SM_DISPLAYNAME      ${LANG_ENGLISH}     "SubMirror real-time subtitle translation"

LangString SM_WELCOME_TITLE    ${LANG_SIMPCHINESE} "安装 SubMirror ${APP_VERSION}"
LangString SM_WELCOME_TITLE    ${LANG_ENGLISH}     "Install SubMirror ${APP_VERSION}"

LangString SM_WELCOME_TEXT     ${LANG_SIMPCHINESE} "SubMirror 会在内存中截取屏幕并采集扬声器输出，用于实时字幕翻译。$\r$\n$\r$\n本程序不做游戏进程注入、不读取游戏内存、不挂钩游戏、不模拟键鼠，也不安装驱动或服务。全部捕获均使用 Windows 用户态公开 API。$\r$\n$\r$\n本安装包按当前用户安装到 %LOCALAPPDATA%，不需要管理员权限。"
LangString SM_WELCOME_TEXT     ${LANG_ENGLISH}     "SubMirror captures screen pixels and the speaker output in memory to produce real-time subtitles.$\r$\n$\r$\nIt does NOT inject into game processes, read game memory, hook the game, simulate input, or install drivers or services. Every capture path uses public Windows user-mode APIs.$\r$\n$\r$\nThis package installs per-user into %LOCALAPPDATA% and needs no administrator rights."

LangString SM_RUN_TEXT         ${LANG_SIMPCHINESE} "立即运行 SubMirror"
LangString SM_RUN_TEXT         ${LANG_ENGLISH}     "Run SubMirror now"

LangString SM_FINISH_LINK      ${LANG_SIMPCHINESE} "项目主页"
LangString SM_FINISH_LINK      ${LANG_ENGLISH}     "Project home page"

LangString SM_SEC_CORE         ${LANG_SIMPCHINESE} "核心程序（必需）"
LangString SM_SEC_CORE         ${LANG_ENGLISH}     "Core program (required)"

LangString SM_SEC_STARTMENU    ${LANG_SIMPCHINESE} "开始菜单快捷方式"
LangString SM_SEC_STARTMENU    ${LANG_ENGLISH}     "Start menu shortcuts"

LangString SM_SEC_DESKTOP      ${LANG_SIMPCHINESE} "桌面快捷方式"
LangString SM_SEC_DESKTOP      ${LANG_ENGLISH}     "Desktop shortcut"

LangString SM_DESC_CORE        ${LANG_SIMPCHINESE} "主程序与运行时依赖库，不可取消。语音识别模型不随安装包分发，请在设置面板中选择已有的模型目录。"
LangString SM_DESC_CORE        ${LANG_ENGLISH}     "Program files and bundled runtime. The speech model is not shipped; pick an existing model folder in the settings panel."

LangString SM_DESC_STARTMENU   ${LANG_SIMPCHINESE} "在开始菜单中创建 SubMirror 文件夹（含启动与卸载快捷方式）。"
LangString SM_DESC_STARTMENU   ${LANG_ENGLISH}     "Create a SubMirror folder in the Start menu (launch and uninstall shortcuts)."

LangString SM_DESC_DESKTOP     ${LANG_SIMPCHINESE} "在桌面上创建启动快捷方式。"
LangString SM_DESC_DESKTOP     ${LANG_ENGLISH}     "Create a launch shortcut on the desktop."

LangString SM_UNINSTALL_SHORTCUT ${LANG_SIMPCHINESE} "卸载 SubMirror"
LangString SM_UNINSTALL_SHORTCUT ${LANG_ENGLISH}     "Uninstall SubMirror"

; Intentionally bilingual: shown from .onInit, i.e. before the language dialog.
LangString SM_RUNNING          ${LANG_SIMPCHINESE} "SubMirror 正在运行，安装程序无法替换正在使用的文件。$\r$\n请先关闭 SubMirror（包括托盘图标），然后点击“重试”。$\r$\n$\r$\nSubMirror is running. Close it first (including the tray icon), then click Retry."
LangString SM_RUNNING          ${LANG_ENGLISH}     "SubMirror is running. Close it first (including the tray icon), then click Retry.$\r$\n$\r$\nSubMirror 正在运行，请先关闭后再点击“重试”。"

LangString SM_RUNNING_SILENT   ${LANG_SIMPCHINESE} "SubMirror 正在运行，无法进行静默安装/卸载。"
LangString SM_RUNNING_SILENT   ${LANG_ENGLISH}     "SubMirror is running; silent install/uninstall aborted."

LangString SM_ABORTED          ${LANG_SIMPCHINESE} "安装已被用户中止。"
LangString SM_ABORTED          ${LANG_ENGLISH}     "Setup was cancelled by the user."

LangString SM_WINVER           ${LANG_SIMPCHINESE} "SubMirror 需要 Windows 10 或更高版本。"
LangString SM_WINVER           ${LANG_ENGLISH}     "SubMirror requires Windows 10 or later."

LangString SM_REMOVE_CONFIG    ${LANG_SIMPCHINESE} "是否同时删除用户配置？$\r$\n$\r$\n位置：%APPDATA%\SubMirror（热键、字幕样式、语音模型路径等）。$\r$\n选择“否”将保留配置，重新安装后设置仍然有效。"
LangString SM_REMOVE_CONFIG    ${LANG_ENGLISH}     "Also delete the user configuration?$\r$\n$\r$\nLocation: %APPDATA%\SubMirror (hotkeys, subtitle style, speech model path).$\r$\nChoose No to keep it, so a later reinstall keeps your settings."

LangString SM_WARN_DIR         ${LANG_SIMPCHINESE} "是否删除整个安装目录？$\r$\n$\r$\n位置：$INSTDIR$\r$\n$\r$\n注意：如果您把语音识别模型放在这个目录里，模型文件也会被一并删除。选择“否”则只删除程序自身文件。"
LangString SM_WARN_DIR         ${LANG_ENGLISH}     "Delete the whole install folder?$\r$\n$\r$\nLocation: $INSTDIR$\r$\n$\r$\nWarning: if you stored the speech model inside this folder it will be deleted too. Choose No to remove only the program files."

; -----------------------------------------------------------------------------
; Running-instance guard
;   Reuses the mutex the application itself creates, so install/uninstall cannot
;   delete files that are still loaded. The mutex name is passed through a
;   variable ("w r0") instead of a literal, which avoids the System plugin's
;   backslash escaping rules.
; -----------------------------------------------------------------------------
!macro SM_CHECK_RUNNING
  StrCpy $R0 "${MUTEX_NAME}"
  ${Do}
    ; SYNCHRONIZE (0x00100000) is enough to test for existence.
    System::Call 'kernel32::OpenMutexW(i 0x00100000, i 0, w r0) i .r9'
    ${If} $9 = 0
      ${Break}
    ${EndIf}
    System::Call 'kernel32::CloseHandle(i r9)'
    ${IfNot} ${Silent}
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "$(SM_RUNNING)" IDRETRY sm_running_retry
      Abort "$(SM_ABORTED)"
    ${EndIf}
    Abort "$(SM_RUNNING_SILENT)"
    sm_running_retry:
  ${Loop}
!macroend

Function .onInit
  ; Per-user context: shortcuts, $APPDATA and $DESKTOP resolve to this account.
  SetShellVarContext current

  ; Language picker (persisted under HKCU\Software\SubMirror\Installer Language).
  !insertmacro MUI_LANGDLL_DISPLAY

  ${IfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "$(SM_WINVER)"
    Abort
  ${EndIf}

  !insertmacro SM_CHECK_RUNNING
FunctionEnd

Function un.onInit
  SetShellVarContext current
  !insertmacro SM_CHECK_RUNNING
FunctionEnd

; -----------------------------------------------------------------------------
; Sections
; -----------------------------------------------------------------------------
Section "$(SM_SEC_CORE)" SEC_CORE
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; Upgrade hygiene: drop the previous PyInstaller payload so stale DLLs and
  ; extension modules cannot shadow the new ones. Anything the user placed
  ; outside _internal (e.g. a model folder) is left untouched.
  ${If} ${FileExists} "$INSTDIR\_internal\*.*"
    DetailPrint "Removing previous runtime payload ($INSTDIR\_internal)"
    RMDir /r "$INSTDIR\_internal"
  ${EndIf}

  SetOverwrite on
  ; Fails at COMPILE time when the PyInstaller payload is missing - early and clear.
  File /r "${DIST_DIR}\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "${INSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${INSTALL_KEY}" "Version"         "${APP_VERSION}"
SectionEnd

Section "$(SM_SEC_STARTMENU)" SEC_STARTMENU
  !insertmacro MUI_STARTMENU_WRITE_BEGIN Application
    CreateDirectory "$SMPROGRAMS\$StartMenuFolder"
    CreateShortCut "$SMPROGRAMS\$StartMenuFolder\${APP_NAME}.lnk" \
                   "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
    CreateShortCut "$SMPROGRAMS\$StartMenuFolder\$(SM_UNINSTALL_SHORTCUT).lnk" \
                   "$INSTDIR\Uninstall.exe"
  !insertmacro MUI_STARTMENU_WRITE_END
SectionEnd

Section "$(SM_SEC_DESKTOP)" SEC_DESKTOP
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" \
                 "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

; Hidden bookkeeping section: "Apps & features" entry.
Section -RegisterUninstall
  WriteRegStr   HKCU "${UNINSTALL_KEY}" "DisplayName"         "$(SM_DISPLAYNAME)"
  WriteRegStr   HKCU "${UNINSTALL_KEY}" "DisplayVersion"      "${APP_VERSION}"
  WriteRegStr   HKCU "${UNINSTALL_KEY}" "Publisher"           "${APP_PUBLISHER}"
  WriteRegStr   HKCU "${UNINSTALL_KEY}" "DisplayIcon"         "$INSTDIR\${APP_EXE}"
  WriteRegStr   HKCU "${UNINSTALL_KEY}" "InstallLocation"     "$INSTDIR"
  WriteRegStr   HKCU "${UNINSTALL_KEY}" "URLInfoAbout"        "${APP_URL}"
  WriteRegStr   HKCU "${UNINSTALL_KEY}" "UninstallString"     '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify"            1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair"            1

  ; Real on-disk size in KB, shown in "Apps & features".
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" $0
SectionEnd

Section "Uninstall"
  !insertmacro SM_CHECK_RUNNING

  ; --- shortcuts -------------------------------------------------------------
  Delete "$DESKTOP\${APP_NAME}.lnk"

  !insertmacro MUI_STARTMENU_GETFOLDER Application $StartMenuFolder
  Delete "$SMPROGRAMS\$StartMenuFolder\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\$StartMenuFolder\$(SM_UNINSTALL_SHORTCUT).lnk"
  RMDir  "$SMPROGRAMS\$StartMenuFolder"

  ; --- user data (kept by default; silent uninstall also keeps it) -----------
  MessageBox MB_YESNO|MB_ICONQUESTION "$(SM_REMOVE_CONFIG)" /SD IDNO IDYES sm_del_config
  Goto sm_keep_config
  sm_del_config:
    ; A portable-mode install keeps its config in $INSTDIR, removed below.
    RMDir /r "$APPDATA\SubMirror"
  sm_keep_config:

  ; --- program files ---------------------------------------------------------
  MessageBox MB_YESNO|MB_ICONQUESTION "$(SM_WARN_DIR)" /SD IDYES IDYES sm_rmdir
    ; User declined: remove only the files this installer created.
    Delete "$INSTDIR\Uninstall.exe"
    Delete "$INSTDIR\${APP_EXE}"
    RMDir /r "$INSTDIR\_internal"
    Goto sm_after_files
  sm_rmdir:
    RMDir /r "$INSTDIR"
  sm_after_files:

  ; Non-recursive: succeeds only if the folder is now empty, so a user-owned
  ; leftover never gets wiped silently.
  RMDir "$INSTDIR"

  ; --- registry --------------------------------------------------------------
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${INSTALL_KEY}"
SectionEnd

; -----------------------------------------------------------------------------
; Section descriptions
; -----------------------------------------------------------------------------
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_CORE}      "$(SM_DESC_CORE)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_STARTMENU} "$(SM_DESC_STARTMENU)"
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP}   "$(SM_DESC_DESKTOP)"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ChatterboxTTS Installer Script for Inno Setup
; Build: ISCC.exe installer.iss
; NOTE: This file is processed from the build_stage directory.
;        All paths are relative to that directory.

#define MyAppName "ChatterboxTTS"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ChatterboxTTS Project"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=.\output
OutputBaseFilename=ChatterboxTTS-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\python\pythonw.exe
PrivilegesRequired=lowest
AllowCancelDuringInstall=False

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "chatterbox_gui.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "launcher.pyw"; DestDir: "{app}"; Flags: ignoreversion
Source: "interface.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "icon.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "config\*"; DestDir: "{app}\config"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "modules\*"; DestDir: "{app}\modules"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "utils\*"; DestDir: "{app}\utils"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "wrapper\*"; DestDir: "{app}\wrapper"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "ASR\*"; DestDir: "{app}\ASR"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Voice_Samples\*"; DestDir: "{app}\Voice_Samples"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: ".env.template"; DestDir: "{app}"; DestName: ".env"; \
  Flags: ignoreversion onlyifdoesntexist
; Bundled Python embeddable (configured for pip)
Source: "python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs


[Icons]
Name: "{group}\{#MyAppName}"; \
  Filename: "{app}\python\python.exe"; \
  Parameters: """{app}\launcher.pyw"""; \
  WorkingDir: "{app}"; \
  IconFilename: "{app}\icon.png"; \
  Comment: "Text-to-Speech GUI Application"
Name: "{userdesktop}\{#MyAppName}"; \
  Filename: "{app}\python\python.exe"; \
  Parameters: """{app}\launcher.pyw"""; \
  WorkingDir: "{app}"; \
  IconFilename: "{app}\icon.png"; \
  Tasks: desktopicon; \
  Comment: "Text-to-Speech GUI Application"

[Run]
Filename: "{app}\python\python.exe"; \
  Parameters: """{app}\launcher.pyw"""; \
  WorkingDir: "{app}"; \
  Description: "Launch {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent
Filename: "notepad.exe"; Parameters: "{app}\.env"; \
  Description: "Configure your HuggingFace token (required for model download)"; \
  Flags: postinstall shellexec skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
Type: dirifempty; Name: "{localappdata}\{#MyAppName}"

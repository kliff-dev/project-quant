#define AppVersion "0.8.1"
#define RootPath AddBackslash(SourcePath) + "..\"

[Setup]
AppId={{B80AF075-C15A-4DDC-B1FD-7C86E28D56D8}
AppName=Project Quant
AppVersion={#AppVersion}
AppPublisher=Project Quant
DefaultDirName={localappdata}\Programs\ProjectQuant
DefaultGroupName=Project Quant
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#RootPath}release
OutputBaseFilename=ProjectQuant-Setup-{#AppVersion}-x64
SetupIconFile={#RootPath}desktop\assets\ProjectQuant.ico
UninstallDisplayIcon={app}\ProjectQuant.exe
WizardStyle=modern
Compression=lzma2/fast
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "{#RootPath}dist\ProjectQuant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Project Quant"; Filename: "{app}\ProjectQuant.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Project Quant"; Filename: "{app}\ProjectQuant.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ProjectQuant.exe"; Description: "Open Project Quant"; Flags: nowait postinstall skipifsilent

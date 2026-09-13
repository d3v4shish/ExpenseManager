#define AppName "ExpenseManager"
#define AppVersion "0.2.0"
#define AppPublisher "ExpenseManager"
#define AppExeName "ExpenseManager.exe"

[Setup]
AppId={{7AC71945-D6F6-49EC-95F8-3BD58AE15A21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\ExpenseManager
DefaultGroupName=ExpenseManager
OutputDir=..\..\dist\installer
OutputBaseFilename=ExpenseManager-{#AppVersion}-Setup-x64
SetupIconFile=..\..\assets\icons\expense_manager_matte.ico
UninstallDisplayIcon={app}\{#AppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\..\dist\ExpenseManager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ExpenseManager"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall ExpenseManager"; Filename: "{uninstallexe}"
Name: "{autodesktop}\ExpenseManager"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Code]
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{app}\{#AppExeName}'), '--uninstall', '', SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode);
  Result := Result and (ResultCode = 0);
end;

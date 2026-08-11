!include "LogicLib.nsh"

!macro customUnInstall
  IfFileExists "$INSTDIR\resources\collector\openusage-collector.exe" 0 usagehub_collector_missing

  nsExec::ExecToStack '"$INSTDIR\resources\collector\openusage-collector.exe" service uninstall'
  Pop $R0
  Pop $R1
  ${If} $R0 != "0"
    Abort "UsageHub background service could not be removed."
  ${EndIf}

  Goto usagehub_lifecycle_done

  usagehub_collector_missing:
  Abort "UsageHub background service could not be removed."

  usagehub_lifecycle_done:
!macroend

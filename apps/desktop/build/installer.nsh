; NSIS include for the Kinora Windows installer.
;
; Registers the kinora:// URL protocol in the per-user registry so deep links
; resolve to the installed app even before its first launch (the runtime
; setAsDefaultProtocolClient call then keeps it current). Cleaned up on
; uninstall. electron-builder's NSIS target wires these macros automatically.

!macro customInstall
  DetailPrint "Registering kinora:// protocol handler"
  WriteRegStr HKCU "Software\Classes\kinora" "" "URL:Kinora Protocol"
  WriteRegStr HKCU "Software\Classes\kinora" "URL Protocol" ""
  WriteRegStr HKCU "Software\Classes\kinora\DefaultIcon" "" "$INSTDIR\${APP_EXECUTABLE_FILENAME},0"
  WriteRegStr HKCU "Software\Classes\kinora\shell\open\command" "" '"$INSTDIR\${APP_EXECUTABLE_FILENAME}" "%1"'
!macroend

!macro customUnInstall
  DetailPrint "Removing kinora:// protocol handler"
  DeleteRegKey HKCU "Software\Classes\kinora"
!macroend

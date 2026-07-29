; 卸载策略（02 章 §3 / 08 章 TODO(M5)）：
; 卸载器询问「是否保留解析数据」——默认保留并提示目录位置，用户明确选「是」才删除。
;
; 两个关键判断：
; - ${isUpdated}：版本升级会先跑一遍旧版卸载器，这时既不能问也不能删，
;   否则每次升级都弹窗/丢数据。仅真正卸载时才进入询问。
; - /SD IDNO：静默卸载（enterprise 脚本、winget 等场景）走默认值「保留」，
;   与交互模式的默认按钮一致——任何默认路径都不删用户数据。
!macro customUnInstall
  ${ifNot} ${isUpdated}
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
      "是否同时删除已解析的文档数据？$\r$\n$\r$\n数据目录：$LOCALAPPDATA\DocFactory$\r$\n$\r$\n选「否」将保留数据，重新安装后可继续使用。" \
      /SD IDNO IDYES uninstDeleteData
    DetailPrint "已保留数据目录：$LOCALAPPDATA\DocFactory"
    Goto uninstDataDone
  uninstDeleteData:
    RMDir /r "$LOCALAPPDATA\DocFactory"
    DetailPrint "已删除数据目录：$LOCALAPPDATA\DocFactory"
  uninstDataDone:
  ${endIf}
!macroend

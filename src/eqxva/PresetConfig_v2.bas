' =====================================================================
'  PresetConfig module - full version 2
'
'  Labels on each block:
'    [EXISTING] = same as what you typed before (skip if already there)
'    [CHANGED]  = replace your old version with this one
'    [NEW]      = type in fresh
'
'  DELETE from your module if you typed them last time:
'    Const CURVE_NAME, Function SetCurveRow, Function SplitList,
'    Function HeaderCol
'
'  Comment lines (starting with ') are optional - no need to type them.
' =====================================================================
Option Explicit

' ---------------- CONSTANTS ----------------
Const XVA_SHEET As String = "XVA Pricer"                                ' [EXISTING]
Const LABEL_PAIRS As String = "F:G:I,M:N:P,S:T:V"                      ' [CHANGED] label:override:used
Const MAX_ROWS As Long = 300                                             ' [EXISTING]
' [CHANGED] = ALL override cells. Used by Clear, Export and the audit log.
' Check the rows on your sheet. Leave out Run # and column S (tenors).
Const CLEAR_RANGES As String = "'XVA Pricer'!G5:G30,'XVA Pricer'!N3:N72," & _
                               "'XVA Pricer'!T4:T9,'XVA Pricer'!T71:T72"
Public Const PRESET_DIR As String = "\\ibgmstorfile01\group1\CVA Desk\Presets\"   ' [EXISTING]
Const BASELINE_SHEET As String = "_PresetBaseline"                       ' [EXISTING]
Const CURVE_COLS As String = "S:T:V"                                     ' [NEW] tenor:override:used
Const CURVE_TITLES As String = "LGD over time,Internal Rating over time," & _
    "External Rating over time,Security over time,Std RW over time"     ' [NEW]
Const DEFAULT_TENORS As String = "0,1,2,5,10,20,50"                      ' [NEW]

Dim mLog As String                                                       ' [EXISTING]
Dim mPresetVals As Object                                                ' [EXISTING]


' =====================================================================
'  BUTTONS
' =====================================================================

' [EXISTING]
Sub LoadPreset()
    With Application.FileDialog(msoFileDialogFilePicker)
        .Title = "Choose XVA preset"
        .InitialFileName = PRESET_DIR
        .Filters.Clear
        .Filters.Add "Preset", "*.yaml;*.yml"
        .AllowMultiSelect = False
        If .Show = -1 Then ApplyPresetFile .SelectedItems(1), True
    End With
End Sub

' [EXISTING]
Sub ClearAllOverrides()
    If MsgBox("Clear all overrides on XVA Pricer?" & vbLf & _
              "Formula cells and Run # are not affected.", _
              vbYesNo + vbExclamation, "Clear overrides") <> vbYes Then Exit Sub
    Application.ScreenUpdating = False
    ClearOverrides
    Application.Calculate
    Application.ScreenUpdating = True
    On Error Resume Next
    ThisWorkbook.Names("loaded_preset").RefersToRange.Value = _
        "(overrides cleared) | " & Format$(Now, "yyyy-mm-dd hh:nn") & " | " & Environ$("USERNAME")
    On Error GoTo 0
    MsgBox "Overrides cleared.", vbInformation
End Sub

' [CHANGED] skips tenor rows, only exports cells inside CLEAR_RANGES,
'           adds [curve] sections at the end
Sub ExportPreset()
    Dim f As Variant, sh As Worksheet, p As Variant, cols() As String
    Dim r As Long, c As Range, lbl As String, fn As Integer, keyTxt As String
    Dim t As Variant, r0 As Long, nn As Long, rr As Long, tn() As String, cs() As String
    Dim curT As String, defT As String, hdr As Boolean, hasV As Boolean

    f = Application.GetSaveAsFilename(PRESET_DIR & "new_preset.yaml", "Preset (*.yaml),*.yaml")
    If VarType(f) = vbBoolean Then Exit Sub
    Set sh = ThisWorkbook.Worksheets(XVA_SHEET)
    fn = FreeFile
    Open CStr(f) For Output As #fn
    Print #fn, "clear_overrides: true"
    Print #fn, XVA_SHEET & ":"

    ' label-based overrides
    For Each p In Split(LABEL_PAIRS, ",")
        cols = Split(p, ":")
        For r = 1 To MAX_ROWS
            Set c = sh.Range(cols(1) & r)
            If Not c.HasFormula And Not IsEmpty(c.Value) And Not IsError(c.Value) Then
                If Not IsTenor(sh.Range(cols(0) & r)) And InOverrideRanges(c) Then
                    lbl = Trim$(sh.Range(cols(0) & r).Text)
                    If lbl = "" Then lbl = cols(1) & r
                    Print #fn, "  " & Chr$(34) & lbl & Chr$(34) & ": " & YamlValue(c.Value)
                End If
            End If
        Next r
    Next p

    ' term-structure blocks
    cs = Split(CURVE_COLS, ":")
    tn = Split(DEFAULT_TENORS, ",")
    For Each t In Split(CURVE_TITLES, ",")
        hdr = False
        If FindCurveBlock(sh, CStr(t), r0, nn) Then
            For rr = 0 To nn - 1
                curT = CStr(sh.Range(cs(0) & (r0 + rr)).Value)
                defT = curT
                If rr <= UBound(tn) Then defT = tn(rr)
                Set c = sh.Range(cs(1) & (r0 + rr))
                hasV = Not c.HasFormula And Not IsEmpty(c.Value) And Not IsError(c.Value)
                If hasV Or defT <> curT Then
                    If Not hdr Then Print #fn, XVA_SHEET & " [curve " & t & "]:": hdr = True
                    keyTxt = defT
                    If defT <> curT Then keyTxt = defT & " > " & curT
                    If hasV Then
                        Print #fn, "  " & keyTxt & ": " & YamlValue(c.Value)
                    Else
                        Print #fn, "  " & keyTxt & ":"
                    End If
                End If
            Next rr
        End If
    Next t

    Close #fn
    MsgBox "Preset saved: " & f & vbLf & vbLf & _
           "Remove trade-specific lines (counterparty, dates, request ID) before reuse." & vbLf & _
           "[cells] changes (e.g. Lifetime EL) are not exported - add them by hand."
End Sub


' =====================================================================
'  LOADER
' =====================================================================

' [CHANGED] dictionary reset, ApplyEntry call, audit stamp + log
Function ApplyPresetFile(ByVal path As String, Optional showLog As Boolean = False) As Boolean
    Dim lines() As String, i As Long, s As String, k As String, v As String
    Dim sect As String, nOk As Long, nBad As Long, fn As Integer, txt As String, nm As String

    fn = FreeFile
    Open path For Binary Access Read As #fn
    txt = Space$(LOF(fn))
    Get #fn, , txt
    Close #fn
    If Left$(txt, 3) = Chr$(239) & Chr$(187) & Chr$(191) Then txt = Mid$(txt, 4)
    lines = Split(Replace(txt, vbCr, ""), vbLf)
    mLog = ""
    Set mPresetVals = CreateObject("Scripting.Dictionary")
    Application.ScreenUpdating = False

    For i = 0 To UBound(lines)
        s = StripComment(lines(i))
        If Trim$(s) <> "" Then
            If Not SplitKV(Trim$(s), k, v) Then
                AddLog i + 1, "cannot read line": nBad = nBad + 1
            ElseIf Left$(s, 1) <> " " Then
                If LCase$(k) = "clear_overrides" Then
                    If LCase$(v) = "true" Then ClearOverrides
                ElseIf v = "" Then
                    sect = k
                End If
            ElseIf sect = "" Then
                AddLog i + 1, "no sheet section above": nBad = nBad + 1
            ElseIf ApplyEntry(sect, k, v, i + 1) Then
                nOk = nOk + 1
            Else
                nBad = nBad + 1
            End If
        End If
    Next i

    Application.ScreenUpdating = True
    Application.Calculate

    nm = Mid$(path, InStrRev(path, "\") + 1)
    On Error Resume Next
    ThisWorkbook.Names("loaded_preset").RefersToRange.Value = _
        nm & " | file " & Format$(FileDateTime(path), "yyyy-mm-dd hh:nn") & _
        " | loaded " & Format$(Now, "yyyy-mm-dd hh:nn") & " | " & Environ$("USERNAME") & _
        IIf(nBad > 0, " | " & nBad & " problems", "")
    On Error GoTo 0
    LogPreset nm, nOk, nBad

    ApplyPresetFile = (nBad = 0)
    If showLog Or nBad > 0 Then
        MsgBox nOk & " settings applied, " & nBad & " problems" & vbLf & vbLf & Left$(mLog, 900)
    End If
End Function

' [CHANGED] routes plain / [cells] / [curve <title>] sections
Function ApplyEntry(sect As String, key As String, v As String, ln As Long) As Boolean
    Dim sheetName As String, mode As String, p As Long
    p = InStr(sect, "[")
    If p = 0 Then ApplyEntry = SetValue(sect, key, v, ln): Exit Function
    sheetName = Trim$(Left$(sect, p - 1))
    mode = Application.Trim(Replace(Mid$(sect, p + 1), "]", ""))
    If LCase$(mode) = "cells" Then
        ApplyEntry = SetCell(sheetName, key, v, ln)
    ElseIf LCase$(Left$(mode, 6)) = "curve " Then
        ApplyEntry = SetCurvePoint(sheetName, Trim$(Mid$(mode, 7)), key, v, ln)
    Else
        AddLog ln, "unknown section type [" & mode & "]"
    End If
End Function

' [EXISTING] plain section: label -> override column (never overwrites formulas)
Function SetValue(sect As String, key As String, v As String, ln As Long) As Boolean
    Dim sh As Worksheet, c As Range
    On Error Resume Next
    Set sh = ThisWorkbook.Worksheets(sect)
    On Error GoTo 0
    If sh Is Nothing Then AddLog ln, "sheet not found: " & sect: Exit Function

    Set c = FindTarget(sh, key)
    If c Is Nothing Then AddLog ln, "not found: " & key: Exit Function
    Set c = c.Cells(1, 1).MergeArea.Cells(1, 1)
    If c.HasFormula Then AddLog ln, "skipped formula cell " & c.Address(0, 0): Exit Function

    On Error GoTo Fail
    c.Value = ToValue(v)
    mPresetVals(sh.Name & "!" & c.Address(0, 0)) = c.Value
    SetValue = True
    Exit Function
Fail:
    AddLog ln, "cannot write " & c.Address(0, 0)
End Function

' [EXISTING] label (with | aliases) -> named range -> address
Function FindTarget(sh As Worksheet, ByVal key As String) As Range
    Dim p As Variant, alt As Variant, cols() As String, arr As Variant, r As Long
    If sh.Name = XVA_SHEET Then
        For Each alt In Split(key, "|")
            For Each p In Split(LABEL_PAIRS, ",")
                cols = Split(p, ":")
                arr = sh.Range(cols(0) & "1").Resize(MAX_ROWS, 1).Value
                For r = 1 To MAX_ROWS
                    If Not IsError(arr(r, 1)) Then
                        If LCase$(Trim$(CStr(arr(r, 1)))) = LCase$(Trim$(alt)) Then
                            Set FindTarget = sh.Range(cols(1) & r)
                            Exit Function
                        End If
                    End If
                Next r
            Next p
        Next alt
    End If
    On Error Resume Next
    Set FindTarget = ThisWorkbook.Names(key).RefersToRange
    If FindTarget Is Nothing Then Set FindTarget = sh.Range(key)
    On Error GoTo 0
End Function

' [EXISTING] [cells] section: exact cell, may replace formulas
Function SetCell(sheetName As String, key As String, v As String, ln As Long) As Boolean
    Dim sh As Worksheet, c As Range
    On Error Resume Next
    Set sh = ThisWorkbook.Worksheets(sheetName)
    Set c = ThisWorkbook.Names(key).RefersToRange
    If c Is Nothing Then Set c = sh.Range(key)
    On Error GoTo 0
    If sh Is Nothing Then AddLog ln, "sheet not found: " & sheetName: Exit Function
    If c Is Nothing Then AddLog ln, "cell not found: " & key: Exit Function
    SetCell = WriteCell(c.Cells(1, 1).MergeArea.Cells(1, 1), v, ln)
End Function

' [CHANGED] added optional useBaseline
Function WriteCell(c As Range, ByVal v As String, ln As Long, Optional useBaseline As Boolean = True) As Boolean
    Dim x As Variant
    If useBaseline Then RememberBaseline c
    x = ToValue(v)
    On Error GoTo Fail
    If VarType(x) = vbString Then
        If Left$(x, 1) = "=" Then c.Formula = x Else c.Value = x
    Else
        c.Value = x
    End If
    If Not mPresetVals Is Nothing Then mPresetVals(c.Worksheet.Name & "!" & c.Address(0, 0)) = c.Formula
    WriteCell = True
    Exit Function
Fail:
    AddLog ln, "cannot write " & c.Address(0, 0) & " - " & Err.Description
End Function


' =====================================================================
'  TERM-STRUCTURE (CURVE) BLOCKS
' =====================================================================

' [NEW] [curve <title>] section line:  tenor: value  /  old > new: value
Function SetCurvePoint(sheetName As String, title As String, key As String, v As String, ln As Long) As Boolean
    Dim sh As Worksheet, cs() As String, r0 As Long, n As Long, r As Long
    Dim fromT As String, toT As String, p As Long, hit As Boolean, ok As Boolean
    On Error Resume Next
    Set sh = ThisWorkbook.Worksheets(sheetName)
    On Error GoTo 0
    If sh Is Nothing Then AddLog ln, "sheet not found: " & sheetName: Exit Function
    If Not FindCurveBlock(sh, title, r0, n) Then AddLog ln, "curve block not found: " & title: Exit Function
    cs = Split(CURVE_COLS, ":")
    p = InStr(key, ">")
    If p > 0 Then
        fromT = Trim$(Left$(key, p - 1)): toT = Trim$(Mid$(key, p + 1))
    Else
        fromT = Trim$(key)
    End If
    ok = True
    For r = r0 To r0 + n - 1
        If TenorMatches(sh.Range(cs(0) & r).Value, fromT) Then
            hit = True
            If toT <> "" Then
                If Not WriteCell(sh.Range(cs(0) & r), toT, ln, False) Then ok = False
            End If
            If Trim$(v) <> "" Then
                If Not WriteCell(sh.Range(cs(1) & r), v, ln, False) Then ok = False
            End If
        End If
    Next r
    If Not hit Then AddLog ln, "tenor " & fromT & " not found in " & title: ok = False
    SetCurvePoint = ok
End Function

' [NEW] finds a block by its title in column S; returns first tenor row and count
Function FindCurveBlock(sh As Worksheet, ByVal title As String, firstRow As Long, nRows As Long) As Boolean
    Dim cs() As String, r As Long, s As Long, t As String
    cs = Split(CURVE_COLS, ":")
    title = LCase$(Trim$(title))
    If title = "" Then Exit Function
    For r = 1 To MAX_ROWS
        t = LCase$(Trim$(sh.Range(cs(0) & r).Text))
        If Left$(t, Len(title)) = title Then
            For s = r + 1 To r + 10
                If IsTenor(sh.Range(cs(0) & s)) Then Exit For
            Next s
            If s > r + 10 Then Exit Function
            firstRow = s: nRows = 0
            Do While IsTenor(sh.Range(cs(0) & (firstRow + nRows))) And nRows < 30
                nRows = nRows + 1
            Loop
            FindCurveBlock = True
            Exit Function
        End If
    Next r
End Function

' [NEW]
Function IsTenor(c As Range) As Boolean
    If IsError(c.Value) Or IsEmpty(c.Value) Then Exit Function
    IsTenor = (VarType(c.Value) <> vbString) And IsNumeric(c.Value)
End Function

' [EXISTING]
Function TenorMatches(cellVal As Variant, ByVal key As String) As Boolean
    If IsError(cellVal) Or IsEmpty(cellVal) Then Exit Function
    key = Trim$(key)
    If key = "*" Then TenorMatches = True: Exit Function
    If IsNumeric(cellVal) And IsNumeric(key) Then
        TenorMatches = Abs(CDbl(cellVal) - CDbl(key)) < 0.0001
    Else
        TenorMatches = (LCase$(Trim$(CStr(cellVal))) = LCase$(key))
    End If
End Function

' [NEW] resets tenors to defaults and clears curve override cells
Sub ClearCurves()
    Dim sh As Worksheet, cs() As String, t As Variant, tn() As String
    Dim r0 As Long, n As Long, r As Long
    Set sh = ThisWorkbook.Worksheets(XVA_SHEET)
    cs = Split(CURVE_COLS, ":")
    tn = Split(DEFAULT_TENORS, ",")
    For Each t In Split(CURVE_TITLES, ",")
        If FindCurveBlock(sh, CStr(t), r0, n) Then
            For r = 0 To n - 1
                With sh.Range(cs(0) & (r0 + r))
                    If Not .HasFormula And r <= UBound(tn) Then .Value = CDbl(tn(r))
                End With
                With sh.Range(cs(1) & (r0 + r))
                    If Not .HasFormula Then .ClearContents
                End With
            Next r
        End If
    Next t
End Sub


' =====================================================================
'  CLEARING + BASELINE
' =====================================================================

' [CHANGED] restores [cells] originals and resets curves first
Sub ClearOverrides()
    Dim a As Variant, c As Range
    RestoreBaseline
    ClearCurves
    On Error Resume Next
    For Each a In Split(CLEAR_RANGES, ",")
        For Each c In Application.Range(CStr(a)).Cells
            If Not c.HasFormula And Not IsEmpty(c.Value) Then c.MergeArea.ClearContents
        Next c
    Next a
    On Error GoTo 0
End Sub

' [NEW] is this cell one of the override cells listed in CLEAR_RANGES?
Function InOverrideRanges(c As Range) As Boolean
    Dim a As Variant, rg As Range
    For Each a In Split(CLEAR_RANGES, ",")
        Set rg = Nothing
        On Error Resume Next
        Set rg = Application.Range(CStr(a))
        On Error GoTo 0
        If Not rg Is Nothing Then
            If rg.Worksheet.Name = c.Worksheet.Name Then
                If Not Intersect(c, rg) Is Nothing Then InOverrideRanges = True: Exit Function
            End If
        End If
    Next a
End Function

' [EXISTING]
Sub RememberBaseline(c As Range)
    Dim ws As Worksheet, k As String, r As Long, last As Long
    Set ws = GetBaselineSheet()
    k = c.Worksheet.Name & "!" & c.Address(0, 0)
    last = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    For r = 2 To last
        If ws.Cells(r, 1).Value = k Then Exit Sub
    Next r
    ws.Cells(last + 1, 1).Value = k
    ws.Cells(last + 1, 2).Value = "'" & c.Formula
    ws.Cells(last + 1, 3).Value = Now
End Sub

' [EXISTING]
Function GetBaselineSheet() As Worksheet
    Dim ws As Worksheet, act As Object
    On Error Resume Next
    Set ws = ThisWorkbook.Worksheets(BASELINE_SHEET)
    On Error GoTo 0
    If ws Is Nothing Then
        Set act = ActiveSheet
        Set ws = ThisWorkbook.Worksheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        ws.Name = BASELINE_SHEET
        ws.Range("A1:C1").Value = Array("Cell", "Original", "First changed")
        ws.Visible = xlSheetHidden
        act.Activate
    End If
    Set GetBaselineSheet = ws
End Function

' [EXISTING]
Sub RestoreBaseline()
    Dim ws As Worksheet, r As Long, last As Long, k As String, p As Long
    On Error Resume Next
    Set ws = ThisWorkbook.Worksheets(BASELINE_SHEET)
    On Error GoTo 0
    If ws Is Nothing Then Exit Sub
    last = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    For r = 2 To last
        k = ws.Cells(r, 1).Value
        p = InStrRev(k, "!")
        If p > 0 Then ThisWorkbook.Worksheets(Left$(k, p - 1)).Range(Mid$(k, p + 1)).Formula = ws.Cells(r, 2).Value
    Next r
    If last >= 2 Then ws.Rows("2:" & last).Delete
End Sub


' =====================================================================
'  AUDIT
' =====================================================================

' [EXISTING]
Sub LogPreset(fileName As String, nOk As Long, nBad As Long)
    Dim ws As Worksheet, r As Long
    On Error Resume Next
    Set ws = ThisWorkbook.Worksheets("Preset Log")
    On Error GoTo 0
    If ws Is Nothing Then Exit Sub
    r = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row + 1
    ws.Cells(r, 1).Resize(1, 5).Value = Array(Now, Environ$("USERNAME"), fileName, nOk, nBad)
End Sub

' [CHANGED] three parts: label overrides, [cells] changes, curve blocks
Sub SnapshotOverrides(reqNo As Long, presetName As String, tradeLabel As String)
    Dim ws As Worksheet, sh As Worksheet, p As Variant, cols() As String
    Dim r As Long, c As Range, k As String, src As String, used As String
    Dim nr As Long, stamp As Date
    Dim bs As Worksheet, b As Long, cur As Range, kk As String, pp As Long
    Dim t As Variant, r0 As Long, nn As Long, rr As Long, tn() As String, cs() As String
    Dim lbl As String, tCell As Range

    On Error Resume Next
    Set ws = ThisWorkbook.Worksheets("Override Log")
    Set bs = ThisWorkbook.Worksheets(BASELINE_SHEET)
    On Error GoTo 0
    If ws Is Nothing Then Exit Sub
    Set sh = ThisWorkbook.Worksheets(XVA_SHEET)
    stamp = Now
    nr = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row + 1

    ' 1) label-based overrides
    For Each p In Split(LABEL_PAIRS, ",")
        cols = Split(p, ":")
        For r = 1 To MAX_ROWS
            Set c = sh.Range(cols(1) & r)
            If Not c.HasFormula And Not IsEmpty(c.Value) And Not IsError(c.Value) Then
                If Not IsTenor(sh.Range(cols(0) & r)) And InOverrideRanges(c) Then
                    k = sh.Name & "!" & c.Address(0, 0)
                    src = "manual"
                    If Not mPresetVals Is Nothing Then
                        If mPresetVals.Exists(k) Then
                            If CStr(mPresetVals(k)) = CStr(c.Value) Then src = "preset" Else src = "preset-edited"
                        End If
                    End If
                    used = ""
                    If UBound(cols) >= 2 Then used = sh.Range(cols(2) & r).Text
                    ws.Cells(nr, 1).Resize(1, 11).Value = Array(stamp, Environ$("USERNAME"), reqNo, _
                        tradeLabel, presetName, Trim$(sh.Range(cols(0) & r).Text), k, c.Value, used, _
                        src, ThisWorkbook.Name)
                    nr = nr + 1
                End If
            End If
        Next r
    Next p

    ' 2) [cells] changes (from baseline sheet)
    If Not bs Is Nothing Then
        For b = 2 To bs.Cells(bs.Rows.Count, 1).End(xlUp).Row
            kk = bs.Cells(b, 1).Value
            pp = InStrRev(kk, "!")
            If pp > 0 Then
                Set cur = ThisWorkbook.Worksheets(Left$(kk, pp - 1)).Range(Mid$(kk, pp + 1))
                src = SourceOf(kk, cur)
                If src = "manual" Then src = "preset (earlier load)"
                ws.Cells(nr, 1).Resize(1, 11).Value = Array(stamp, Environ$("USERNAME"), reqNo, _
                    tradeLabel, presetName, "was: " & bs.Cells(b, 2).Value, kk, _
                    "'" & cur.Formula, cur.Text, src, ThisWorkbook.Name)
                nr = nr + 1
            End If
        Next b
    End If

    ' 3) curve overrides and tenor changes
    cs = Split(CURVE_COLS, ":")
    tn = Split(DEFAULT_TENORS, ",")
    For Each t In Split(CURVE_TITLES, ",")
        If FindCurveBlock(sh, CStr(t), r0, nn) Then
            For rr = 0 To nn - 1
                Set tCell = sh.Range(cs(0) & (r0 + rr))
                Set cur = sh.Range(cs(1) & (r0 + rr))
                lbl = t & " / tenor " & tCell.Text
                If rr <= UBound(tn) Then
                    If CDbl(tn(rr)) <> tCell.Value Then lbl = lbl & " (default " & tn(rr) & ")"
                End If
                If (Not cur.HasFormula And Not IsEmpty(cur.Value)) Or InStr(lbl, "(default") > 0 Then
                    kk = sh.Name & "!" & cur.Address(0, 0)
                    src = SourceOf(kk, cur)
                    If src = "manual" Then src = SourceOf(sh.Name & "!" & tCell.Address(0, 0), tCell)
                    ws.Cells(nr, 1).Resize(1, 11).Value = Array(stamp, Environ$("USERNAME"), reqNo, _
                        tradeLabel, presetName, lbl, kk, cur.Value, sh.Range(cs(2) & (r0 + rr)).Text, _
                        src, ThisWorkbook.Name)
                    nr = nr + 1
                End If
            Next rr
        End If
    Next t
End Sub

' [NEW]
Function SourceOf(k As String, c As Range) As String
    SourceOf = "manual"
    If mPresetVals Is Nothing Then Exit Function
    If mPresetVals.Exists(k) Then
        If CStr(mPresetVals(k)) = CStr(c.Formula) Then SourceOf = "preset" Else SourceOf = "preset-edited"
    End If
End Function


' =====================================================================
'  BATCH HELPERS
' =====================================================================

' [EXISTING]
Function PresetForRequest(i As Long) As String
    Dim s As String, r As Range, rw As Range
    s = Trim$(BatchSheet.Range("D7").Offset(0, i).Text)
    If s <> "" Then PresetForRequest = s: Exit Function
    On Error Resume Next
    Set r = ThisWorkbook.Names("preset_plan").RefersToRange
    On Error GoTo 0
    If r Is Nothing Then Exit Function
    For Each rw In r.Rows
        If Trim$(rw.Cells(1, 3).Text) <> "" And IsNumeric(rw.Cells(1, 1).Value) _
           And IsNumeric(rw.Cells(1, 2).Value) Then
            If i >= rw.Cells(1, 1).Value And i <= rw.Cells(1, 2).Value Then
                PresetForRequest = Trim$(rw.Cells(1, 3).Text): Exit Function
            End If
        End If
    Next rw
End Function

' [EXISTING]
Function PresetPath(ByVal nm As String) As String
    If InStr(nm, "\") = 0 Then nm = PRESET_DIR & nm
    If LCase$(Right$(nm, 5)) <> ".yaml" And LCase$(Right$(nm, 4)) <> ".yml" Then nm = nm & ".yaml"
    PresetPath = nm
End Function


' =====================================================================
'  TEXT HELPERS  (all [EXISTING])
' =====================================================================

Function ToValue(ByVal v As String) As Variant
    v = Trim$(v)
    If v = "" Or v = "~" Then ToValue = Empty: Exit Function
    If Len(v) >= 2 And (Left$(v, 1) = Chr$(34) Or Left$(v, 1) = "'") And Right$(v, 1) = Left$(v, 1) Then
        ToValue = Mid$(v, 2, Len(v) - 2): Exit Function
    End If
    If LCase$(v) = "true" Then ToValue = True: Exit Function
    If LCase$(v) = "false" Then ToValue = False: Exit Function
    If Right$(v, 1) = "%" And IsNumeric(Left$(v, Len(v) - 1)) Then
        ToValue = CDbl(Left$(v, Len(v) - 1)) / 100
    ElseIf IsNumeric(v) Then
        ToValue = CDbl(v)
    Else
        ToValue = v
    End If
End Function

Function SplitKV(ByVal s As String, k As String, v As String) As Boolean
    Dim q As String, p As Long
    q = Left$(s, 1)
    If q = Chr$(34) Or q = "'" Then
        p = InStr(2, s, q)
        If p = 0 Then Exit Function
        k = Mid$(s, 2, p - 2)
        s = LTrim$(Mid$(s, p + 1))
        If Left$(s, 1) <> ":" Then Exit Function
        v = Trim$(Mid$(s, 2))
    Else
        p = InStr(s, ": ")
        If p > 0 Then
            k = Trim$(Left$(s, p - 1))
            v = Trim$(Mid$(s, p + 2))
        ElseIf Right$(s, 1) = ":" Then
            k = Trim$(Left$(s, Len(s) - 1))
            v = ""
        Else
            Exit Function
        End If
    End If
    SplitKV = (k <> "")
End Function

Function StripComment(ByVal s As String) As String
    Dim i As Long, ch As String, q As String
    For i = 1 To Len(s)
        ch = Mid$(s, i, 1)
        If q = "" Then
            If ch = Chr$(34) Or ch = "'" Then
                q = ch
            ElseIf ch = "#" And (i = 1 Or Mid$(s, i - 1, 1) = " ") Then
                StripComment = RTrim$(Left$(s, i - 1)): Exit Function
            End If
        ElseIf ch = q Then
            q = ""
        End If
    Next i
    StripComment = RTrim$(s)
End Function

Function YamlValue(v As Variant) As String
    Select Case VarType(v)
        Case vbBoolean: YamlValue = LCase$(CStr(v))
        Case vbDate: YamlValue = Format$(v, "yyyy-mm-dd")
        Case vbString: YamlValue = Chr$(34) & Replace(v, Chr$(34), "'") & Chr$(34)
        Case Else: YamlValue = CStr(v)
    End Select
End Function

Sub AddLog(ln As Long, msg As String)
    mLog = mLog & "Line " & ln & ": " & msg & vbLf
End Sub


' =====================================================================
'  REFERENCE ONLY - lines that live in RunBatch (Automation module).
'  Unchanged from before; listed so you can check they are in place.
' =====================================================================
'
'  Above  For i = fromReq To toReq :
'      Dim curPreset As String, wantPreset As String
'      For i = fromReq To toReq
'          wantPreset = PresetForRequest(i)
'          If wantPreset <> "" Then
'              If Dir$(PresetPath(wantPreset)) = "" Then
'                  MsgBox "Preset file missing for request " & i & ":" & vbLf & PresetPath(wantPreset)
'                  Exit Sub
'              End If
'          End If
'      Next i
'
'  First lines inside the loop:
'      wantPreset = PresetForRequest(i)
'      If wantPreset <> "" And wantPreset <> curPreset Then
'          If Not ApplyPresetFile(PresetPath(wantPreset)) Then
'              MsgBox "Preset problem at request " & i & " - batch stopped"
'              Exit Sub
'          End If
'          curPreset = wantPreset
'      End If
'
'  After the line ending  .value = Now :
'      With Range("BatchResultsStart").Offset(-10, (i - 1 - (fromReq - 1)) * 2)
'          .ClearComments
'          If curPreset <> "" Then .AddComment "Preset: " & curPreset
'      End With
'
'  Right after  Call PriceCVA :
'      SnapshotOverrides i, curPreset, BatchSheet.Range("BatchStart").Offset(i, 1).Value

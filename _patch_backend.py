import io
p = r'D:\Desktop\ai报告生成\ai报告生成\backend\app\routers\projects.py'
src = io.open(p, encoding='utf-8').read()
old_imp = 'from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile'
new_imp = 'from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, UploadFile'
assert src.count(old_imp) == 1, 'import line not found'
src = src.replace(old_imp, new_imp)
anchor = '''@router.get("/{project_id}/report")
def download_report(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = get_own_project(project_id, user, db)
    if not project.report_path:
        raise HTTPException(status_code=404, detail="尚未导出报告，请先点击“导出报告”")
    return {"url": storage.presigned_url(project.report_path)}
'''
assert src.count(anchor) == 1, 'anchor not found'
addition = anchor + '''

@router.get("/{project_id}/report/preview")
def preview_report(project_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project = get_own_project(project_id, user, db)
    if not project.report_path:
        raise HTTPException(status_code=404, detail="尚未导出报告，请先点击“导出报告”")
    return Response(
        content=storage.download_bytes(project.report_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
'''
src = src.replace(anchor, addition)
io.open(p, 'w', encoding='utf-8').write(src)
print('patched projects.py')

"""Read only PDF bytes returned by web_fetch; no network or fallback facts."""
import base64
import io


def source_text(source):
    if not isinstance(source,dict):return ''
    if source.get('type')=='text':
        return source.get('data','') if isinstance(source.get('data'),str) else ''
    if source.get('type')!='base64' or source.get('media_type')!='application/pdf':return ''
    data=source.get('data')
    if not isinstance(data,str) or len(data)>1400000:return ''
    try:
        raw=base64.b64decode(data,validate=True)
        if not raw.startswith(b'%PDF-') or len(raw)>1000000:return ''
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            if not 1<=len(pdf.pages)<=20:return ''
            text='\n'.join(page.extract_text() or '' for page in pdf.pages)
        return text if len(text)<=250000 else ''
    except Exception:
        # Missing dependency or unreadable PDF never becomes evidence.
        return ''

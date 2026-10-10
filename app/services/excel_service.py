import pandas as pd
from fastapi import UploadFile
from sqlalchemy.orm import Session
from app.models.models import ExcelFile, ExcelRow
from app.db.base_repo import BaseRepository
import io
from fastapi import HTTPException

class ExcelService:
    """
    Excel 处理服务类
    """
    def __init__(self, db: Session):
        self.db = db
        self.file_repo = BaseRepository(ExcelFile)
        self.row_repo = BaseRepository(ExcelRow)

    async def upload_and_save(self, file: UploadFile):
        """
        接收并解析 Excel 文件，保存到数据库
        """
        contents = await file.read()
        
        # 使用 pandas 读取任意结构的 Excel（默认只读取第一个sheet）
        if file.filename.endswith('.xlsx'):
            df = pd.read_excel(io.BytesIO(contents), engine='openpyxl', sheet_name=0)
        else:
            df = pd.read_excel(io.BytesIO(contents), engine='xlrd', sheet_name=0)

        # 1. 保存文件元数据
        db_file = ExcelFile(filename=file.filename)
        self.db.add(db_file)
        self.db.flush() # 获取生成的 ID

        # 2. 转换每一行为 JSON 并保存
        # df.to_dict('records') 会将每一行转为一个字典，列名为键
        rows_data = df.to_dict('records')
        
        for index, row_content in enumerate(rows_data):
            # 处理可能存在的 NaN (JSON 不支持 NaN)
            clean_row = {k: (None if pd.isna(v) else v) for k, v in row_content.items()}
            db_row = ExcelRow(
                file_id=db_file.id,
                row_index=index,
                data=clean_row
            )
            self.db.add(db_row)
        
        self.db.commit()
        self.db.refresh(db_file)
        return db_file

    def get_file_data(self, file_id: int):
        """
        读取特定文件的行数据，供其他接口调用
        """
        file_info = self.db.query(ExcelFile).filter(ExcelFile.id == file_id).first()
        if not file_info:
            raise HTTPException(status_code=404, detail="文件不存在")
        return file_info.filename, self.db.query(ExcelRow).filter(ExcelRow.file_id == file_id).all()

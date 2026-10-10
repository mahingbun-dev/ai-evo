"""model.py 声明解析器（AST 静态解析，不执行用户代码）

约定：脚本目录内的 model.py 用 pydantic 声明脚本的输入/输出契约——

    from pydantic import BaseModel, Field

    class PreScriptInput(BaseModel):
        \"\"\"输入字段声明（平台解析后写入脚本记录的 input_fields）\"\"\"
        msg: str = Field(..., description="待处理文本（inline 或本地文件路径）")
        extra_file: str | None = Field(
            None, description="可选附加文件路径",
            json_schema_extra={"value_type": "local"})

    class Pattern(BaseModel):
        pattern: str = Field(..., description="模式串")

    class PreScriptOutput(BaseModel):
        \"\"\"标准输出 output 的键声明（平台解析后写入脚本记录的 output_schema）\"\"\"
        text: str = Field(..., description="归一化后的文本")
        patterns: list[Pattern] = Field(..., description="命中明细")

解析产物对齐平台既有契约（BUILTIN_SCRIPT_DEFS 风格）：
- input_fields = {字段名: {"value_type": inline/local/oss/hub_file, "description": ...}}（保持平铺）
- output_schema = {键: 值}，值有两种形态：
  - 叶子："{类型注解}：{说明}" 串（无说明时仅注解），兼容存量数据；
  - 嵌套：注解引用模块内自定义 BaseModel 类（直接引用 / list[Model] /
    dict[str, Model] / Optional[Model] / Model | None / 字符串前向引用）时，
    展开为 {"type": "object"|"array", "description": ..., "children": {子键: 值(递归)}}。
  顶层键始终是真实输出键（运行时 output.get(key) 单级取用），嵌套细节只进值内。

任何解析失败（语法错误、类缺失、字段形态不支持、循环引用、超深嵌套）都不抛
异常：对应节返回 None / 降级为叶子串，并收集 warnings 由调用方透出——保存
model.py 永不因解析失败被阻断。
"""
import ast
from typing import Any, Dict, List, Optional, Tuple

# 约定文件名与类名（模板与文档同步维护）
MODEL_SCHEMA_FILENAME = "model.py"
INPUT_CLASS_NAME = "PreScriptInput"
OUTPUT_CLASS_NAME = "PreScriptOutput"

# input_fields 的合法取值类型（与数据集字段落地通道一致）
VALID_VALUE_TYPES = ("inline", "local", "oss", "hub_file")

# 嵌套展开安全上限：超过按叶子处理（防病态深递归）
MAX_NESTING_DEPTH = 8


def parse_model_schemas(source: str) -> Dict[str, Any]:
    """解析 model.py 源码中的 PreScriptInput / PreScriptOutput 声明

    返回 {"input_fields": dict|None, "output_schema": dict|None, "warnings": [str]}：
    对应类不存在 / 无可解析字段时该节为 None；语法错误时两节均 None。
    """
    warnings: List[str] = []
    result: Dict[str, Any] = {"input_fields": None, "output_schema": None, "warnings": warnings}
    if not (source or "").strip():
        result["warnings"].append(f"{MODEL_SCHEMA_FILENAME} 内容为空，未解析声明")
        return result
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        result["warnings"].append(f"{MODEL_SCHEMA_FILENAME} 存在语法错误（第 {e.lineno} 行: {e.msg}），未解析声明")
        return result

    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    input_cls = classes.get(INPUT_CLASS_NAME)
    if input_cls is not None:
        fields = _parse_class_fields(input_cls, INPUT_CLASS_NAME, warnings)
        if fields:
            result["input_fields"] = {
                name: {"value_type": extra.get("value_type", "inline"),
                       "description": extra.get("description", "")}
                for name, (_anno, extra) in fields
            }
    # 输入类缺省不警告（允许只声明输出的 model.py）

    output_cls = classes.get(OUTPUT_CLASS_NAME)
    if output_cls is not None:
        fields = _parse_class_fields(output_cls, OUTPUT_CLASS_NAME, warnings)
        if fields:
            class_map = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
            result["output_schema"] = {
                name: _resolve_output_value(
                    anno, extra, class_map,
                    frozenset({INPUT_CLASS_NAME, OUTPUT_CLASS_NAME}), warnings)
                for name, (anno, extra) in fields
            }
    return result


def validate_model_py_source(source: str) -> Optional[str]:
    """导入通道强校验：model.py 源码必须语法可解析且两个契约类声明齐全

    返回 None 表示通过；否则返回中文错误描述（语法错误含行号，缺类指明缺哪个）。
    与 parse_model_schemas 的静默容忍语义不同：导入必须能区分「语法错」与
    「缺类」并阻断，故独立校验而不复用其返回值。字段允许为空（空壳类通过，
    只校验声明存在性，不要求字段声明）。
    """
    if not (source or "").strip():
        return f"{MODEL_SCHEMA_FILENAME} 内容为空"
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return f"{MODEL_SCHEMA_FILENAME} 语法错误（第 {e.lineno} 行: {e.msg}）"
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    missing = [name for name in (INPUT_CLASS_NAME, OUTPUT_CLASS_NAME) if name not in classes]
    if missing:
        return f"{MODEL_SCHEMA_FILENAME} 缺少契约类声明: {'、'.join(missing)}"
    return None


def _parse_class_fields(cls_node: ast.ClassDef, class_name: str,
                        warnings: List[str]) -> List[Tuple[str, Tuple[str, Dict[str, Any]]]]:
    """提取 pydantic 模型类中带注解的字段：[(字段名, (类型注解, {description, value_type}))]"""
    fields: List[Tuple[str, Tuple[str, Dict[str, Any]]]] = []
    for node in cls_node.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        try:
            anno = ast.unparse(node.annotation)
        except Exception:
            anno = "Any"
        extra: Dict[str, Any] = {}
        value = node.value
        if value is None:
            fields.append((name, (anno, extra)))
            continue
        if isinstance(value, ast.Call):
            desc, vt, is_field = _extract_field_call(value)
            if not is_field:
                # 动态默认值：字段保留（名与注解确定），但说明无法静态取到
                warnings.append(f"{class_name}.{name} 的默认值不是 Field(...) 形态，未取到字段说明")
                fields.append((name, (anno, extra)))
                continue
            if desc is not None:
                extra["description"] = desc
            if vt is not None:
                if vt in VALID_VALUE_TYPES:
                    extra["value_type"] = vt
                else:
                    warnings.append(
                        f"{class_name}.{name} 的 value_type={vt!r} 不在 {VALID_VALUE_TYPES}，按 inline 处理")
            fields.append((name, (anno, extra)))
            continue
        # 非 Field 的常量默认值（如 x: str = "abc"）：字段保留，说明留空
        if isinstance(value, ast.Constant):
            fields.append((name, (anno, extra)))
        else:
            warnings.append(f"{class_name}.{name} 的默认值不是可解析形态（需 Field(...) 或常量），已跳过该字段")
    return fields


def _extract_field_call(call: ast.Call) -> Tuple[Optional[str], Optional[str], bool]:
    """从 Field(...) 调用中提取 (description, value_type, 是否为 Field 调用)

    - description: keyword description 的字符串常量
    - value_type: keyword json_schema_extra 字典中 value_type 键的字符串常量
    - is_field=False 表示调用名不是 Field（动态默认值等）：字段保留但无法
      静态取到说明，由调用方记 warning 提示
    """
    func = call.func
    if not (isinstance(func, ast.Name) and func.id == "Field"):
        return None, None, False
    desc: Optional[str] = None
    vt: Optional[str] = None
    for kw in call.keywords:
        if kw.arg == "description" and isinstance(kw.value, ast.Constant) \
                and isinstance(kw.value.value, str):
            desc = kw.value.value
        elif kw.arg == "json_schema_extra" and isinstance(kw.value, ast.Dict):
            for key, val in zip(kw.value.keys, kw.value.values):
                if isinstance(key, ast.Constant) and key.value == "value_type" \
                        and isinstance(val, ast.Constant) and isinstance(val.value, str):
                    vt = val.value
    return desc, vt, True


def _is_base_model_class(cls_node: ast.ClassDef) -> bool:
    """判断类是否继承 BaseModel（直接或 pydantic.BaseModel 属性形态）"""
    for base in cls_node.bases:
        if isinstance(base, ast.Name) and base.id == "BaseModel":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
            return True
    return False


def _unwrap_optional(anno: ast.expr) -> Optional[ast.expr]:
    """解包 Optional[X] / Union 成员含 None 的注解；非 Optional 返回 None"""
    if isinstance(anno, ast.Subscript) and isinstance(anno.value, ast.Name) \
            and anno.value.id in ("Optional", "Union"):
        return anno.slice
    if isinstance(anno, ast.BinOp) and isinstance(anno.op, ast.BitOr):
        parts = [anno.left, anno.right]
        non_none = [p for p in parts
                    if not (isinstance(p, ast.Constant) and p.value is None)]
        if len(non_none) == 1 and len(non_none) != len(parts):
            return non_none[0]
    return None


def _resolve_output_value(anno: str, extra: Dict[str, Any], class_map: Dict[str, ast.ClassDef],
                          visiting: frozenset, warnings: List[str],
                          depth: int = 0) -> Any:
    """把输出字段注解解析为 output_schema 值（叶子串或嵌套 dict，形态见模块 docstring）

    anno 为 _parse_class_fields 产出的注解字符串。引用模块内 BaseModel 子类
    （直接 / list[Model] / dict[str, Model] / Optional 包装 / 前向引用字符串）
    时展开 children；循环引用、非 BaseModel 类、超深嵌套、不可解析注解一律
    降级为既有叶子串并不抛异常。
    """
    desc = extra.get("description", "")

    def leaf(note: Optional[str] = None) -> str:
        if note:
            warnings.append(note)
        return f"{anno}：{desc}" if desc else anno

    if depth > MAX_NESTING_DEPTH:
        return leaf(f"输出声明嵌套超过 {MAX_NESTING_DEPTH} 层，按叶子处理：{anno}")

    # 注解串 → AST（字符串前向引用剥引号后同样按表达式解析）
    expr = anno.strip()
    if len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in ("'", '"'):
        expr = expr[1:-1]
    try:
        node = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return leaf()

    # Optional/Union-with-None 解包：解到类则展开，否则按原注解串降级叶子
    inner = _unwrap_optional(node)
    if inner is not None:
        inner_anno = ast.unparse(inner)
        resolved = _resolve_output_value(inner_anno, extra, class_map, visiting, warnings, depth + 1)
        if isinstance(resolved, dict):
            return resolved
        return leaf()

    # 类引用（Name / 前向引用字符串）：展开为 object + children
    if isinstance(node, ast.Name) and node.id in class_map:
        return _expand_model_class(node.id, "object", desc, class_map, visiting, warnings, depth)

    # list[Model] / dict[str, Model]：元素为类时展开对应容器
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        head = node.value.id
        elem = None
        if head in ("list", "List", "Sequence", "MutableSequence", "tuple", "Tuple"):
            elem = node.slice
        elif head == "dict" and isinstance(node.slice, ast.Tuple) and len(node.slice.elts) == 2:
            elem = node.slice.elts[1]
        if elem is not None:
            elem_anno = ast.unparse(elem)
            resolved = _resolve_output_value(elem_anno, {"description": ""}, class_map, visiting, warnings, depth + 1)
            if isinstance(resolved, dict):
                container = "array" if head in ("list", "List", "Sequence", "MutableSequence", "tuple", "Tuple") else "object"
                value: Dict[str, Any] = {"type": container, "children": resolved["children"]}
                if desc:
                    value["description"] = desc
                return value
    return leaf()


def _expand_model_class(class_name: str, type_name: str, desc: str,
                        class_map: Dict[str, ast.ClassDef], visiting: frozenset,
                        warnings: List[str], depth: int) -> Any:
    """展开一个 BaseModel 子类为 {type, description, children}；循环/非 BaseModel 降级叶子"""
    if class_name in visiting:
        warnings.append(f"输出声明存在循环引用（{class_name}），按叶子处理")
        return f"{type_name}：{desc}" if desc else type_name
    cls_node = class_map[class_name]
    if not _is_base_model_class(cls_node):
        warnings.append(f"输出声明引用了非 BaseModel 类 {class_name}，未展开子结构")
        return f"{class_name}：{desc}" if desc else class_name
    fields = _parse_class_fields(cls_node, class_name, warnings)
    children: Dict[str, Any] = {
        name: _resolve_output_value(anno, extra, class_map, visiting | {class_name}, warnings, depth + 1)
        for name, (anno, extra) in fields
    }
    value: Dict[str, Any] = {"type": type_name, "children": children}
    if desc:
        value["description"] = desc
    return value


def sync_schemas_from_model(script_dir, row, db) -> Dict[str, Any]:
    """读取脚本目录下 model.py 并把解析结果回写到脚本记录（model.py 恒为准）

    由 update_script_file / import_package / create_from_template 在落盘后调用：
    任一节解析成功即覆盖对应字段；两节均无（无文件 / 语法错 / 类缺失）则不动
    现有值。返回 parse_model_schemas 的结果供调用方透出。
    """
    from pathlib import Path
    model_path = Path(script_dir) / MODEL_SCHEMA_FILENAME
    if not model_path.is_file():
        return {"input_fields": None, "output_schema": None, "warnings": []}
    parsed = parse_model_schemas(model_path.read_text(encoding="utf-8", errors="replace"))
    changed = False
    if parsed["input_fields"]:
        row.input_fields = parsed["input_fields"]
        changed = True
    if parsed["output_schema"]:
        row.output_schema = parsed["output_schema"]
        changed = True
    if changed:
        db.commit()
        db.refresh(row)
    return parsed

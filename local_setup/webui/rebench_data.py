"""Read the small pinned official Parquet source without persisting answer columns in the catalog."""
import hashlib
import io

from .benchmarks import REBENCH_REVISION

SOURCE_URL = f'https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard/resolve/{REBENCH_REVISION}/data/test-00000-of-00001.parquet'
SOURCE_BYTES = 4864925
SOURCE_SHA256 = '1497b218db0a8259d22471812f0de12b779c699daf741b930d68bc717636f0bf'
PUBLIC_COLUMNS = ['instance_id','repo','base_commit','problem_statement','interface','created_at','image_name','docker_image']


def read_source(content: bytes, columns=None):
    import pyarrow.parquet as pq
    if len(content) != SOURCE_BYTES or hashlib.sha256(content).hexdigest() != SOURCE_SHA256:
        raise ValueError('SWE-rebench 固定数据校验失败；未替换目录或保存题目。')
    table = pq.read_table(io.BytesIO(content), columns=columns)
    if table.num_rows != 860:
        raise ValueError('SWE-rebench 固定快照题目数量不匹配。')
    return table


def download_source(emit=None) -> bytes:
    import requests
    if emit:emit('phase','读取 SWE-rebench 固定数据快照（约 4.86 MB），不会下载镜像')
    chunks=[];size=0
    with requests.get(SOURCE_URL,stream=True,timeout=(10,45)) as response:
        response.raise_for_status()
        for chunk in response.iter_content(128*1024):
            size+=len(chunk)
            if size>SOURCE_BYTES:
                raise ValueError('SWE-rebench 数据超过已核实大小，已停止读取。')
            chunks.append(chunk)
    return b''.join(chunks)


def catalog_rows(emit=None) -> list[dict]:
    return read_source(download_source(emit),PUBLIC_COLUMNS).to_pylist()


def load_case(instance: str, emit=None) -> dict:
    import pyarrow.compute as pc
    table=read_source(download_source(emit))
    rows=table.filter(pc.equal(table['instance_id'],instance)).to_pylist()
    if len(rows)!=1:
        raise ValueError('SWE-rebench 固定快照中没有此题号。')
    return rows[0]


def solver_input(row: dict) -> str:
    statement=row['problem_statement']
    interface=row.get('interface') or ''
    return statement + ('\n\nRequired interfaces:\n'+interface if interface.strip() else '')

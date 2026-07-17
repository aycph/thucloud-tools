from typing import List, Union, Optional, TypedDict
from typing_extensions import TypeGuard
import os
from bs4 import BeautifulSoup as bs
from msvcrt import getwch
import sys
import requests
from time import time
import re

def makeCookies(usr: str, psw: str):
    data = {
        'i_user': usr,
        'i_pass': psw
    }
    session = requests.Session()
    session.get('https://cloud.tsinghua.edu.cn')
    r = session.post('https://id.tsinghua.edu.cn/do/off/ui/auth/login/check', data=data)
    redirect_url = re.search(r'window.location.replace\("(?P<url>.*?)"\)', r.text).group("url") # type: ignore
    session.get(redirect_url)
    return session.cookies

PATTERN = r'.*cloud.tsinghua.edu.cn/d/(?P<id>(\d|[a-f]){20}).*'
API_URL = 'https://cloud.tsinghua.edu.cn/api/v2.1/share-links/{id}/dirents/?path={path}'
CHUNKSIZE = 1024*1024

class FolderInfo(TypedDict):
    folder_name: str
    folder_path: str
    is_dir: bool # true
    last_modified: str # e.g. 2022-03-14T21:07:56+08:00
    size: int # 0
class FileInfo(TypedDict):
    file_name: str
    file_path: str
    is_dir: bool # false
    last_modified: str # e.g. 2022-03-14T21:07:56+08:00
    size: int
def isdir(obj: Union[FolderInfo, FileInfo]) -> TypeGuard[FolderInfo]:
    return obj["is_dir"]
def isfile(obj: Union[FolderInfo, FileInfo]) -> TypeGuard[FileInfo]:
    return not obj["is_dir"]
DirentList = TypedDict('DirentList', dirent_list = List[Union[FolderInfo, FileInfo]])

class Bytes:
    def __init__(self, size: int = 0):
        self.bytes = size
    @property
    def bytes(self):
        return self.__bytes
    @bytes.setter
    def bytes(self, size: int):
        if (size < 0):
            raise ValueError('negative size')
        self.__bytes = size
    def __add__(self, other: 'Bytes'):
        return Bytes(self.bytes + self.bytes)
    def __str__(self):
        size = self.bytes
        if size < 1024: return '%d bytes' % size
        elif (size := size / 1024) < 1024: return "%.1f KB" % size
        elif (size := size / 1024) < 1024: return "%.1f MB" % size
        else: return "%.1f GB" % (size / 1024)

class File:
    def __init__(self, file_name: str, file_path: str, size: int, id: str, **_):
        self.__id = id
        self.name = file_name
        self.path = file_path
        self.bytes = Bytes(size)

class Tree:
    def __init__(self, url: Optional[str] = None, *, id: Optional[str] = None, path: str = '/', name: str = ""):
        if id is None:
            if url is None:
                raise TypeError('url and id cannot both be None')
            elif not (m := re.search(PATTERN, url)):
                raise Exception('Unexpected Format of URL')
            result: str = m.group('id')
            id = result
        self.__id = id
        self.path = path
        
        response: DirentList = requests.get(API_URL.format(id=id, path=path)).json()
        dirent_list = response["dirent_list"]
        self.__children = [
            File(id=id,**obj) if isfile(obj) else
            Tree(id=id, path=obj["folder_path"], name=obj["folder_name"]) if isdir(obj) else # 必居其一
            Tree() for obj in dirent_list
        ]
        self.bytes = sum((obj.bytes for obj in self.__children))

#实现Cloud类，对有无参数、缓存、密码分别处理
#处理*.*GB的情况以及KB
#继续处理带有子目录的情况
#首先是统计链接时加入“其中%d个文件夹”
#其次是下载时处理文件夹（可简单通过是否存在大小）
#增加速度的计算
#返回该文件剩余时间和总时间
#分离download部分
#先下载文件名.download，成功后删除后缀
#输出足够多的
#注意不要下载了目前未实现的文件夹
#使用urllib.request.retrieve
#重新研究进度条
#暂停
#图形界面？
"""
    ExitCode:
    1   文件读取失败
    2   未查找到有效链接
    3   主动退出程序
    4   目录创建失败
"""


"""


name = "清华大学云盘.html"
while not os.path.exists(name):
    name = input("文件：%s不存在！请重新指定路径：\n" % name)
    print()

try:
    print("读取中...", end="\r")
    bsObj = bs(open(name, "rb"), "html.parser")
    print("已读取√   \n")
except:
    print("文件处理失败！请检查后重试\n")
    exit(1)

print("查找下载链接中...\r", end="")  # , flush=True)
tr_list = bsObj.find("tbody").findAll("tr")
print("查找完毕！       \n")

rows = len(tr_list)
print("共查找到链接%d个：\n" % rows)
if rows <= 100:
    total_size = Bytes(0)
    for i, tr in enumerate(tr_list):
        tds = tr.findAll("td")
        tr_list[i] = tds
        print('\t' + tds[1].getText(), end="")  # , flush=True)
        if size_str := tds[2].getText():
            print('\t' + size_str)
            total_size += Bytes(size_str)
        else:
            print()
else:
    total_size = Bytes(0)
    for i, tr in enumerate(tr_list):
        tds = tr.findAll("td")
        tr_list[i] = tds
        if size_str := tds[2].getText():
            total_size += Bytes(size_str)
print("共计%s(不含子目录)" % total_size)

print()
print("q键退出程序\n其它键进入下一步\n")
cmd = getwch()
#sys.stdin.flush()
if cmd == 'q':
    exit(3)

path = bsObj.find("div", {"class": "d-flex justify-content-between align-items-center op-bar"}
                  ).find("p", {"class": "m-0"}).getText(separator="", strip=True)[5:]
print("当前路径：%s" % path)
print()
print("0键开始转存\n其它键自定义路径")  # Enter↲
cmd = getwch()
#sys.stdin.flush()
if cmd != '0':
    print()
    path = input("请输入自定义路径：\n")

try:
    if not os.path.exists(path):
        os.makedirs(path)
    print("已创建目录\n")
except:
    print("路径不合法！\n")
    exit(4)

print("开始转存！\n")

for (i, tds) in enumerate(tr_list):
    filepath = path + '\\' + tds[1].getText()
    size = tds[2].getText()
    print("%d/%d\t%-10s%s...\b\b\b" %
          (i+1, rows, size, filepath), end="")  # , flush=True)
    try:
        if os.path.exists(filepath):
            print("\t  ⭕ 文件已存在")
            continue
        fp = open(filepath, "wb")
    except:
        print("\t  × 文件创建失败")
        continue
    try:
        r = requests.get(tds[4].a["href"], stream=True)
        count = -1
        t = time()

        for chunk in r.iter_content(chunk_size=CHUNKSIZE):
            if chunk:
                fp.write(chunk)
                _t = time()
                count += 1
                s = "%dMb/%s" % (count, size)
                if _t != t:
                    print("%20s    %4.2fMb/s\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b\b" %
                          (s, 1/(_t-t)), end="")  # , flush=True)
                    t = _t
    except Exception as error:
        print("\t  × 转存失败                      ")
        print(error)
        fp.close()
        continue
    fp.close()
    print("\t  √                           ")

print()
print("\a任务完成！\n")
exit(0)

"""

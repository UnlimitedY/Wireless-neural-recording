#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
路径工具模块
提供获取应用程序目录和数据保存路径的工具函数

当程序以exe形式运行时，确保所有文件保存在exe所在目录下
"""

import datetime
import os
import sys
from pathlib import Path

def get_application_directory():
    """
    获取应用程序所在目录
    
    Returns:
        str: 应用程序所在的目录路径
        
    Notes:
        - 当以Python脚本运行时，返回脚本所在目录
        - 当以PyInstaller打包的exe运行时，返回exe所在目录
        - 确保在所有情况下都能正确获取应用程序目录
    """
    if getattr(sys, 'frozen', False):
        # 运行在PyInstaller打包的exe中
        application_path = os.path.dirname(sys.executable)
    else:
        # 运行在Python解释器中
        application_path = os.path.dirname(os.path.abspath(__file__))
        # 如果当前文件在neural_recorder_GUI子目录中，返回上级目录
        if os.path.basename(application_path) == 'neural_recorder_GUI':
            application_path = os.path.dirname(application_path)
    
    return application_path

def get_data_directory(subfolder="Data"):
    """
    获取数据保存目录
    
    Args:
        subfolder (str): 子文件夹名称，默认为"Data"
        
    Returns:
        str: 数据保存目录的完整路径
    """
    app_dir = get_application_directory()
    data_dir = os.path.join(app_dir, subfolder)
    
    # 确保目录存在
    os.makedirs(data_dir, exist_ok=True)
    
    return data_dir

def get_recordings_directory():
    """
    获取录像保存目录
    
    Returns:
        str: 录像保存目录的完整路径
    """
    return get_data_directory("recordings")


def build_daily_file_path(base_path, when=None):
    """
    将一个基础文件路径映射到按天分组的子目录中。

    例如:
    - ``C:/data/video/BW11`` -> ``C:/data/video/2026-04-23/BW11``
    - ``C:/data/video/2026-04-23/BW11`` -> 保持不重复嵌套
    """
    candidate = str(base_path or "").strip()
    if not candidate:
        return candidate

    if when is None:
        when = datetime.datetime.now()
    date_str = when.strftime("%Y-%m-%d")

    normalized_dir = os.path.dirname(candidate)
    if os.path.basename(os.path.normpath(normalized_dir)) == date_str:
        daily_dir = normalized_dir
    else:
        daily_dir = os.path.join(normalized_dir, date_str)

    if daily_dir:
        os.makedirs(daily_dir, exist_ok=True)

    return os.path.join(daily_dir, os.path.basename(candidate))

def get_mouse_data_directory(mouse_id):
    """
    获取特定鼠标的数据目录
    
    Args:
        mouse_id (str): 鼠标ID
        
    Returns:
        str: 鼠标数据目录的完整路径
    """
    data_dir = get_data_directory()
    mouse_dir = os.path.join(data_dir, mouse_id)
    
    # 确保目录存在
    os.makedirs(mouse_dir, exist_ok=True)
    
    return mouse_dir

def get_default_save_path(filename="", extension=""):
    """
    获取默认的文件保存路径
    
    Args:
        filename (str): 文件名（不含扩展名）
        extension (str): 文件扩展名（如".txt", ".json"等）
        
    Returns:
        str: 完整的文件保存路径
    """
    data_dir = get_data_directory()
    
    if filename:
        if extension and not extension.startswith('.'):
            extension = '.' + extension
        full_filename = filename + extension
        return os.path.join(data_dir, full_filename)
    else:
        return data_dir

def ensure_directory_exists(file_path):
    """
    确保文件路径的目录存在
    
    Args:
        file_path (str): 文件路径
    """
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

def get_relative_path_from_app(target_path):
    """
    获取相对于应用程序目录的相对路径
    
    Args:
        target_path (str): 目标路径
        
    Returns:
        str: 相对路径
    """
    app_dir = get_application_directory()
    try:
        return os.path.relpath(target_path, app_dir)
    except ValueError:
        # 如果无法计算相对路径（如不同驱动器），返回绝对路径
        return target_path

def get_config_file_path(filename):
    """
    获取配置文件的完整路径
    
    Args:
        filename (str): 配置文件名
        
    Returns:
        str: 配置文件的完整路径
    """
    app_dir = get_application_directory()
    return os.path.join(app_dir, filename)

# 为了向后兼容，提供一些常用的路径
def get_fcf_file_path(filename):
    """
    获取滤波器系数文件(.fcf)的路径
    
    Args:
        filename (str): fcf文件名
        
    Returns:
        str: fcf文件的完整路径
    """
    app_dir = get_application_directory()
    fcf_path = os.path.join(app_dir, filename)
    
    # 如果在应用程序目录找不到，尝试在当前目录查找
    if not os.path.exists(fcf_path):
        current_dir_path = os.path.join(os.getcwd(), filename)
        if os.path.exists(current_dir_path):
            return current_dir_path
    
    return fcf_path

# 调试函数
def print_path_info():
    """
    打印路径信息，用于调试
    """
    print("=== 路径信息 ===")
    print(f"sys.frozen: {getattr(sys, 'frozen', False)}")
    print(f"sys.executable: {sys.executable}")
    print(f"__file__: {__file__}")
    print(f"应用程序目录: {get_application_directory()}")
    print(f"数据目录: {get_data_directory()}")
    print(f"录像目录: {get_recordings_directory()}")
    print("===============")

if __name__ == "__main__":
    # 测试函数
    print_path_info()
    
    # 测试各种路径获取
    print(f"默认保存路径: {get_default_save_path()}")
    print(f"测试文件路径: {get_default_save_path('test', 'txt')}")
    print(f"鼠标数据目录: {get_mouse_data_directory('mouse_001')}")
    print(f"配置文件路径: {get_config_file_path('config.json')}")

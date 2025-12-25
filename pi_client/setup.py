#!/usr/bin/env python
"""
兼容性安装脚本
"""

from setuptools import setup, find_packages

setup(
    name="moyurobot",
    version="1.0.0",
    description="摸鱼遥控车控制系统",
    author="MoYu Team",
    python_requires=">=3.10",
    package_dir={"": "."},
    packages=find_packages(where="."),
    include_package_data=True,
    package_data={
        "moyurobot.web": ["templates/*.html", "static/*.css", "static/*.js"],
    },
    install_requires=[
        "lerobot",  # 核心依赖，来自 https://github.com/huggingface/lerobot
        "flask>=2.3.0",
        "flask-cors>=4.0.0",
        "fastmcp>=0.1.0",
        "websockets>=11.0",
        "opencv-python>=4.8.0",
        "numpy>=1.24.0",
        "python-dotenv>=1.0.0",
        "requests>=2.31.0",
    ],
    entry_points={
        "console_scripts": [
            "moyurobot=moyurobot.cli:main",
        ],
    },
)


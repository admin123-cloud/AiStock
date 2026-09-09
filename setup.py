"""
AiStock - 量化交易AI系统

一个基于Python的量化交易系统，集成了机器学习、深度学习和强化学习模型，
支持多种数据源、交易策略和回测功能。
"""

from pathlib import Path

from setuptools import find_packages, setup

# 读取README文件
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text(encoding="utf-8") if readme_file.exists() else ""

# 读取requirements文件
requirements_file = Path(__file__).parent / "requirements.txt"
requirements = []
if requirements_file.exists():
    requirements = [
        line.strip()
        for line in requirements_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

setup(
    name="aistock",
    version="1.0.0",
    author="AiStock Team",
    author_email="contact@aistock.com",
    description="量化交易AI系统 - 集成机器学习、深度学习和强化学习模型",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/admin123-cloud/AiStock",
    packages=find_packages(exclude=["tests", "tests.*", "*.tests", "*.tests.*"]),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Financial and Insurance Industry",
        "Topic :: Office/Business :: Financial :: Investment",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.10",
    install_requires=requirements,
    extras_require={
        "dev": [
            "pytest>=7.4.0",
            "pytest-cov>=4.1.0",
            "black>=23.0.0",
            "flake8>=7.0.0",
            "mypy>=1.0.0",
        ],
        "ml": [
            "torch>=2.0.0",
            "tensorflow>=2.13.0",
            "scikit-learn>=1.3.0",
            "xgboost>=2.0.0",
            "lightgbm>=4.0.0",
        ],
        "viz": [
            "matplotlib>=3.7.0",
            "seaborn>=0.12.0",
            "plotly>=5.17.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "aistock-research=research.__main__:main",
            "aistock-update=scripts.update_data:main",
            "aistock-live=scripts.live_trading:main",
            "aistock-cron=scripts.cron_jobs:main",
        ],
    },
    include_package_data=True,
    package_data={"research": ["catalog.json"]},
    zip_safe=False,
)

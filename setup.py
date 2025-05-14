import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

SDK_VERSION = "0.1.0" # Initial version for Python SDK

setuptools.setup(
    name="vitrus", # Name as it will be on PyPI
    version=SDK_VERSION,
    author="Vitrus AI", # Replace with actual author if different
    author_email="support@vitrus.ai", # Replace with actual email
    description="Python client for Vitrus multi-agent orchestration platform",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/vitrus-ai/vitrus-sdk-python", # Replace with actual URL
    packages=setuptools.find_packages(), # Finds the 'vitrus' package automatically
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Intended Audience :: Developers",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    python_requires='>=3.7',
    install_requires=[
        "websockets>=10.0", # For WebSocket communication
        "asyncio>=3.4.3",
    ],
    keywords=[
        "vitrus", "actors", "agents", "workflows", "robotics", "ai", 
        "ai-agents", "ai-workflows", "ai-actors", "multi-agent systems"
    ],
    project_urls={
        'Documentation': 'https://vitrus.gitbook.io/docs/concepts',
        'Source': 'https://github.com/vitrus-ai/vitrus-sdk-python', # Replace if different
        'Tracker': 'https://github.com/vitrus-ai/vitrus-sdk-python/issues', # Replace if different
    },
) 
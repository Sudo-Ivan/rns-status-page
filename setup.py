import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

with open("requirements.txt", "r", encoding="utf-8") as f:
    requirements = f.read().splitlines()

setuptools.setup(
    name="rns-status-page",
    version="1.1.2",
    author="Ivan",
    author_email="rns@quad4.io",
    description="A Flask web server to display Reticulum network status.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Sudo-Ivan/rns-status-page",
    packages=['rns_status_page'],
    package_dir={'rns_status_page': 'rns_status_page'},
    include_package_data=True,
    package_data={
        'rns_status_page': [
            'templates/**/*',
            'static/**/*',
        ],
    },
    install_requires=requirements,
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
    entry_points={
        'console_scripts': [
            'rns-status-page=rns_status_page.status_page:main',
        ],
    },
)

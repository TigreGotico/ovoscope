from setuptools import setup, find_packages
import os

BASEDIR = os.path.abspath(os.path.dirname(__file__))

def get_version():
    """ Find the version of the package"""
    version_file = os.path.join(BASEDIR, 'ovoscope', 'version.py')
    major, minor, build, alpha = (None, None, None, None)
    with open(version_file) as f:
        for line in f:
            if 'VERSION_MAJOR' in line:
                major = line.split('=')[1].strip()
            elif 'VERSION_MINOR' in line:
                minor = line.split('=')[1].strip()
            elif 'VERSION_BUILD' in line:
                build = line.split('=')[1].strip()
            elif 'VERSION_ALPHA' in line:
                alpha = line.split('=')[1].strip()

            if ((major and minor and build and alpha) or
                    '# END_VERSION_BLOCK' in line):
                break
    version = f"{major}.{minor}.{build}"
    if alpha and int(alpha) > 0:
        version += f"a{alpha}"
    return version

setup(
    name="ovoscope",
    version=get_version(),
    description="End-to-end test framework for OpenVoiceOS skills",
    long_description=open(f"{BASEDIR}/README.md").read(),
    long_description_content_type="text/markdown",
    author="JarbasAI",
    author_email="jarbasai@mailfence.com",
    url="https://github.com/TigreGotico/ovoscope",
    license="Apache-2.0",
    packages=find_packages(),
    install_requires=[
        "ovos-core>=2.0.4a2"
    ],
    python_requires='>=3.10',
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Testing",
    ],
    include_package_data=True,
    zip_safe=False,
)
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import zipfile
from typing import Callable, Dict, List, NamedTuple, Optional, Union


def mkdir_p(path: str):
    if os.path.exists(path):
        print(f'mkdir -p {path} => already exists')
        return
    os.makedirs(path, exist_ok=True)
    print(f'mkdir -p {path} => directory created')


def read_version_file(path: str) -> Dict[str, str]:
    versions = {}

    lines = open(path).readlines()
    for line in lines:
        line = line.strip()

        # コメント行
        if line[:1] == '#':
            continue

        # 空行
        if len(line) == 0:
            continue

        [a, b] = map(lambda x: x.strip(), line.split('=', 2))
        versions[a] = b.strip('"')

    return versions


def versioned(func):
    def wrapper(version, version_file, *args, **kwargs):
        if 'ignore_version' in kwargs:
            if kwargs.get('ignore_version'):
                rm_rf(version_file)
            del kwargs['ignore_version']

        if os.path.exists(version_file):
            ver = open(version_file).read()
            if ver.strip() == version.strip():
                return

        r = func(version=version, *args, **kwargs)

        with open(version_file, 'w') as f:
            f.write(version)

        return r

    return wrapper


def rm_rf(path: str):
    if not os.path.exists(path):
        print(f'rm -rf {path} => path not found')
        return
    if os.path.isfile(path) or os.path.islink(path):
        os.remove(path)
        print(f'rm -rf {path} => file removed')
    if os.path.isdir(path):
        shutil.rmtree(path)
        print(f'rm -rf {path} => directory removed')


def cmd(args, **kwargs):
    print(f'+{args} {kwargs}')
    if 'check' not in kwargs:
        kwargs['check'] = True
    if 'resolve' in kwargs:
        resolve = kwargs['resolve']
        del kwargs['resolve']
    else:
        resolve = True
    if resolve:
        args = [shutil.which(args[0]), *args[1:]]
    return subprocess.run(args, **kwargs)


def download(url: str, output_dir: Optional[str] = None, filename: Optional[str] = None) -> str:
    if filename is None:
        output_path = urllib.parse.urlparse(url).path.split('/')[-1]
    else:
        output_path = filename

    if output_dir is not None:
        output_path = os.path.join(output_dir, output_path)

    if os.path.exists(output_path):
        return output_path

    try:
        if shutil.which('curl') is not None:
            cmd(["curl", "-fLo", output_path, url])
        else:
            cmd(["wget", "-cO", output_path, url])
    except Exception:
        # ゴミを残さないようにする
        if os.path.exists(output_path):
            os.remove(output_path)
        raise

    return output_path


# アーカイブが単一のディレクトリに全て格納されているかどうかを調べる。
#
# 単一のディレクトリに格納されている場合はそのディレクトリ名を返す。
# そうでない場合は None を返す。
def _is_single_dir(infos: List[Union[zipfile.ZipInfo, tarfile.TarInfo]],
                   get_name: Callable[[Union[zipfile.ZipInfo, tarfile.TarInfo]], str],
                   is_dir: Callable[[Union[zipfile.ZipInfo, tarfile.TarInfo]], bool]) -> Optional[str]:
    # tarfile: ['path', 'path/to', 'path/to/file.txt']
    # zipfile: ['path/', 'path/to/', 'path/to/file.txt']
    # どちらも / 区切りだが、ディレクトリの場合、後ろに / が付くかどうかが違う
    dirname = None
    for info in infos:
        name = get_name(info)
        n = name.rstrip('/').find('/')
        if n == -1:
            # ルートディレクトリにファイルが存在している
            if not is_dir(info):
                return None
            dir = name.rstrip('/')
        else:
            dir = name[0:n]
        # ルートディレクトリに２個以上のディレクトリが存在している
        if dirname is not None and dirname != dir:
            return None
        dirname = dir

    return dirname


def is_single_dir_tar(tar: tarfile.TarFile) -> Optional[str]:
    return _is_single_dir(tar.getmembers(), lambda t: t.name, lambda t: t.isdir())


def is_single_dir_zip(zip: zipfile.ZipFile) -> Optional[str]:
    return _is_single_dir(zip.infolist(), lambda z: z.filename, lambda z: z.is_dir())


# 解凍した上でファイル属性を付与する
def _extractzip(z: zipfile.ZipFile, path: str):
    z.extractall(path)
    if platform.system() == 'Windows':
        return
    for info in z.infolist():
        if info.is_dir():
            continue
        filepath = os.path.join(path, info.filename)
        mod = info.external_attr >> 16
        if (mod & 0o120000) == 0o120000:
            # シンボリックリンク
            with open(filepath, 'r') as f:
                src = f.read()
            os.remove(filepath)
            with cd(os.path.dirname(filepath)):
                if os.path.exists(src):
                    os.symlink(src, filepath)
        if os.path.exists(filepath):
            # 普通のファイル
            os.chmod(filepath, mod & 0o777)


# zip または tar.gz ファイルを展開する。
#
# 展開先のディレクトリは {output_dir}/{output_dirname} となり、
# 展開先のディレクトリが既に存在していた場合は削除される。
#
# もしアーカイブの内容が単一のディレクトリであった場合、
# そのディレクトリは無いものとして展開される。
#
# つまりアーカイブ libsora-1.23.tar.gz の内容が
# ['libsora-1.23', 'libsora-1.23/file1', 'libsora-1.23/file2']
# であった場合、extract('libsora-1.23.tar.gz', 'out', 'libsora') のようにすると
# - out/libsora/file1
# - out/libsora/file2
# が出力される。
#
# また、アーカイブ libsora-1.23.tar.gz の内容が
# ['libsora-1.23', 'libsora-1.23/file1', 'libsora-1.23/file2', 'LICENSE']
# であった場合、extract('libsora-1.23.tar.gz', 'out', 'libsora') のようにすると
# - out/libsora/libsora-1.23/file1
# - out/libsora/libsora-1.23/file2
# - out/libsora/LICENSE
# が出力される。
def extract(file: str, output_dir: str, output_dirname: str, filetype: Optional[str] = None):
    path = os.path.join(output_dir, output_dirname)
    print(f"Extract {file} to {path}")
    if filetype == 'gzip' or file.endswith('.tar.gz'):
        rm_rf(path)
        with tarfile.open(file) as t:
            dir = is_single_dir_tar(t)
            if dir is None:
                os.makedirs(path, exist_ok=True)
                t.extractall(path)
            else:
                print(f"Directory {dir} is stripped")
                path2 = os.path.join(output_dir, dir)
                rm_rf(path2)
                t.extractall(output_dir)
                if path != path2:
                    print(f"mv {path2} {path}")
                    os.replace(path2, path)
    elif filetype == 'zip' or file.endswith('.zip'):
        rm_rf(path)
        with zipfile.ZipFile(file) as z:
            dir = is_single_dir_zip(z)
            if dir is None:
                os.makedirs(path, exist_ok=True)
                # z.extractall(path)
                _extractzip(z, path)
            else:
                print(f"Directory {dir} is stripped")
                path2 = os.path.join(output_dir, dir)
                rm_rf(path2)
                # z.extractall(output_dir)
                _extractzip(z, output_dir)
                if path != path2:
                    print(f"mv {path2} {path}")
                    os.replace(path2, path)
    else:
        raise Exception('file should end with .tar.gz or .zip')


@versioned
def install_webrtc(version, source_dir, install_dir, platform: str):
    win = platform.startswith("windows_")
    filename = f'webrtc.{platform}.{"zip" if win else "tar.gz"}'
    rm_rf(os.path.join(source_dir, filename))
    archive = download(
        f'https://github.com/shiguredo-webrtc-build/webrtc-build/releases/download/{version}/{filename}',
        output_dir=source_dir)
    rm_rf(os.path.join(install_dir, 'webrtc'))
    extract(archive, output_dir=install_dir, output_dirname='webrtc')


class WebrtcInfo(NamedTuple):
    version_file: str
    webrtc_include_dir: str
    webrtc_library_dir: str
    clang_dir: str
    libcxx_dir: str


def get_webrtc_info(webrtcbuild: bool, source_dir: str, build_dir: str, install_dir: str) -> WebrtcInfo:
    webrtc_source_dir = os.path.join(source_dir, 'webrtc')
    webrtc_build_dir = os.path.join(build_dir, 'webrtc')
    webrtc_install_dir = os.path.join(install_dir, 'webrtc')

    if webrtcbuild:
        return WebrtcInfo(
            version_file=os.path.join(source_dir, 'webrtc-build', 'VERSION'),
            webrtc_include_dir=os.path.join(webrtc_source_dir, 'src'),
            webrtc_library_dir=os.path.join(webrtc_build_dir, 'obj')
            if platform.system() == 'Windows' else webrtc_build_dir, clang_dir=os.path.join(
                webrtc_source_dir, 'src', 'third_party', 'llvm-build', 'Release+Asserts'),
            libcxx_dir=os.path.join(webrtc_source_dir, 'src', 'buildtools', 'third_party', 'libc++', 'trunk'),)
    else:
        return WebrtcInfo(
            version_file=os.path.join(webrtc_install_dir, 'VERSIONS'),
            webrtc_include_dir=os.path.join(webrtc_install_dir, 'include'),
            webrtc_library_dir=os.path.join(install_dir, 'webrtc', 'lib'),
            clang_dir=os.path.join(install_dir, 'llvm', 'clang'),
            libcxx_dir=os.path.join(install_dir, 'llvm', 'libcxx'),
        )


class ChangeDirectory(object):
    def __init__(self, cwd):
        self._cwd = cwd

    def __enter__(self):
        self._old_cwd = os.getcwd()
        print(f'pushd {self._old_cwd} --> {self._cwd}')
        os.chdir(self._cwd)

    def __exit__(self, exctype, excvalue, trace):
        print(f'popd {self._old_cwd} <-- {self._cwd}')
        os.chdir(self._old_cwd)
        return False


def cd(cwd):
    return ChangeDirectory(cwd)


def git_clone_shallow(url, hash, dir):
    rm_rf(dir)
    mkdir_p(dir)
    with cd(dir):
        cmd(['git', 'init'])
        cmd(['git', 'remote', 'add', 'origin', url])
        cmd(['git', 'fetch', '--depth=1', 'origin', hash])
        cmd(['git', 'reset', '--hard', 'FETCH_HEAD'])


@versioned
def install_llvm(version, install_dir,
                 tools_url, tools_commit,
                 libcxx_url, libcxx_commit,
                 buildtools_url, buildtools_commit):
    llvm_dir = os.path.join(install_dir, 'llvm')
    rm_rf(llvm_dir)
    mkdir_p(llvm_dir)
    with cd(llvm_dir):
        # tools の update.py を叩いて特定バージョンの clang バイナリを拾う
        git_clone_shallow(tools_url, tools_commit, 'tools')
        with cd('tools'):
            cmd(['python3',
                os.path.join('clang', 'scripts', 'update.py'),
                '--output-dir', os.path.join(llvm_dir, 'clang')])

        # 特定バージョンの libcxx を利用する
        git_clone_shallow(libcxx_url, libcxx_commit, 'libcxx')

        # __config_site のために特定バージョンの buildtools を取得する
        git_clone_shallow(buildtools_url, buildtools_commit, 'buildtools')
        with cd('buildtools'):
            cmd(['git', 'reset', '--hard', buildtools_commit])
        shutil.copyfile(os.path.join(llvm_dir, 'buildtools', 'third_party', 'libc++', '__config_site'),
                        os.path.join(llvm_dir, 'libcxx', 'include', '__config_site'))


@versioned
def install_boost(version, source_dir, install_dir, sora_version, platform: str):
    win = platform.startswith("windows_")
    filename = f'boost-{version}_sora-cpp-sdk-{sora_version}_{platform}.{"zip" if win else "tar.gz"}'
    rm_rf(os.path.join(source_dir, filename))
    archive = download(
        f'https://github.com/shiguredo/sora-cpp-sdk/releases/download/{sora_version}/{filename}',
        output_dir=source_dir)
    rm_rf(os.path.join(install_dir, 'boost'))
    extract(archive, output_dir=install_dir, output_dirname='boost')


@versioned
def install_lyra(version, source_dir, install_dir, sora_version, platform: str):
    win = platform.startswith("windows_")
    filename = f'lyra-{version}_sora-cpp-sdk-{sora_version}_{platform}.{"zip" if win else "tar.gz"}'
    rm_rf(os.path.join(source_dir, filename))
    archive = download(
        f'https://github.com/shiguredo/sora-cpp-sdk/releases/download/{sora_version}/{filename}',
        output_dir=source_dir)
    rm_rf(os.path.join(install_dir, 'lyra'))
    extract(archive, output_dir=install_dir, output_dirname='lyra')


@versioned
def install_sora(version, source_dir, install_dir, platform: str):
    win = platform.startswith("windows_")
    filename = f'sora-cpp-sdk-{version}_{platform}.{"zip" if win else "tar.gz"}'
    rm_rf(os.path.join(source_dir, filename))
    archive = download(
        f'https://github.com/shiguredo/sora-cpp-sdk/releases/download/{version}/{filename}',
        output_dir=source_dir)
    rm_rf(os.path.join(install_dir, 'sora'))
    extract(archive, output_dir=install_dir, output_dirname='sora')


class PlatformTarget(object):
    def __init__(self, os, osver, arch):
        self.os = os
        self.osver = osver
        self.arch = arch

    @property
    def package_name(self):
        if self.os == 'windows':
            return f'windows_{self.arch}'
        if self.os == 'macos':
            return f'macos_{self.arch}'
        if self.os == 'ubuntu':
            return f'ubuntu-{self.osver}_{self.arch}'
        if self.os == 'raspberry-pi-os':
            return f'raspberry-pi-os_{self.arch}'
        if self.os == 'jetson':
            return 'ubuntu-20.04_armv8_jetson'
        raise Exception('error')


def get_build_platform() -> PlatformTarget:
    os = platform.system()
    if os == 'Windows':
        os = 'windows'
        osver = None
    elif os == 'Darwin':
        os = 'macos'
        osver = None
    elif os == 'Linux':
        release = read_version_file('/etc/os-release')
        os = release['NAME']
        if os == 'Ubuntu':
            os = 'ubuntu'
            osver = release['VERSION_ID']
        else:
            raise Exception(f'OS {os} not supported')
        pass
    else:
        raise Exception(f'OS {os} not supported')

    arch = platform.machine()
    if arch in ('AMD64', 'x86_64'):
        arch = 'x86_64'
    elif arch in ('aarch64', 'arm64'):
        arch = 'arm64'
    else:
        raise Exception(f'Arch {arch} not supported')

    return PlatformTarget(os, osver, arch)


def install_deps(platform: PlatformTarget, source_dir, build_dir, install_dir):
    version = read_version_file('VERSION')

    install_webrtc_args = {
        'version': version['WEBRTC_BUILD_VERSION'],
        'version_file': os.path.join(install_dir, 'webrtc.version'),
        'source_dir': source_dir,
        'install_dir': install_dir,
        'platform': platform.package_name,
    }
    install_webrtc(**install_webrtc_args)

    webrtc_info = get_webrtc_info(False, source_dir, build_dir, install_dir)
    webrtc_version = read_version_file(webrtc_info.version_file)

    if platform.os == 'ubuntu':
        # LLVM
        tools_url = webrtc_version['WEBRTC_SRC_TOOLS_URL']
        tools_commit = webrtc_version['WEBRTC_SRC_TOOLS_COMMIT']
        libcxx_url = webrtc_version['WEBRTC_SRC_BUILDTOOLS_THIRD_PARTY_LIBCXX_TRUNK_URL']
        libcxx_commit = webrtc_version['WEBRTC_SRC_BUILDTOOLS_THIRD_PARTY_LIBCXX_TRUNK_COMMIT']
        buildtools_url = webrtc_version['WEBRTC_SRC_BUILDTOOLS_URL']
        buildtools_commit = webrtc_version['WEBRTC_SRC_BUILDTOOLS_COMMIT']
        install_llvm_args = {
            'version':
                f'{tools_url}.{tools_commit}.'
                f'{libcxx_url}.{libcxx_commit}.'
                f'{buildtools_url}.{buildtools_commit}',
            'version_file': os.path.join(install_dir, 'llvm.version'),
            'install_dir': install_dir,
            'tools_url': tools_url,
            'tools_commit': tools_commit,
            'libcxx_url': libcxx_url,
            'libcxx_commit': libcxx_commit,
            'buildtools_url': buildtools_url,
            'buildtools_commit': buildtools_commit,
        }
        install_llvm(**install_llvm_args)

    # Boost
    install_boost_args = {
        'version': version['BOOST_VERSION'],
        'version_file': os.path.join(install_dir, 'boost.version'),
        'source_dir': source_dir,
        'install_dir': install_dir,
        'sora_version': version['SORA_CPP_SDK_VERSION'],
        'platform': platform.package_name,
    }
    install_boost(**install_boost_args)

    # Lyra
    install_lyra_args = {
        'version': version['LYRA_VERSION'],
        'version_file': os.path.join(install_dir, 'lyra.version'),
        'source_dir': source_dir,
        'install_dir': install_dir,
        'sora_version': version['SORA_CPP_SDK_VERSION'],
        'platform': platform.package_name,
    }
    install_lyra(**install_lyra_args)

    # Sora C++ SDK
    install_sora_args = {
        'version': version['SORA_CPP_SDK_VERSION'],
        'version_file': os.path.join(install_dir, 'sora.version'),
        'source_dir': source_dir,
        'install_dir': install_dir,
        'platform': platform.package_name,
    }
    install_sora(**install_sora_args)


def cmake_path(path: str) -> str:
    return path.replace('\\', '/')


# 標準出力をキャプチャするコマンド実行。シェルの `cmd ...` や $(cmd ...) と同じ
def cmdcap(args, **kwargs):
    # 3.7 でしか使えない
    # kwargs['capture_output'] = True
    kwargs['stdout'] = subprocess.PIPE
    kwargs['stderr'] = subprocess.PIPE
    kwargs['encoding'] = 'utf-8'
    return cmd(args, **kwargs).stdout.strip()


def run_setup(build_platform, deps_dir):
    try:
        import nanobind  # noqa: F401
        from skbuild import setup
    except ImportError:
        print("このプロジェクトでは 'pip install .' のように 'setup.py' は pip 経由で実行することを推奨しています。"
              "もしセットアップスクリプトを直接実行したい場合は始めに pyproject.toml に記載した依存をインストールしてください！",
              file=sys.stderr)
        raise

    source_dir = os.path.join(deps_dir, '_source')
    build_dir = os.path.join(deps_dir, '_build')
    install_dir = os.path.join(deps_dir, '_install')
    mkdir_p(source_dir)
    mkdir_p(build_dir)
    mkdir_p(install_dir)

    install_deps(build_platform, source_dir, build_dir, install_dir)

    configuration = 'Release'

    webrtc_info = get_webrtc_info(False, source_dir, build_dir, install_dir)

    cmake_args = []
    cmake_args.append(f'-DCMAKE_BUILD_TYPE={configuration}')
    cmake_args.append(f'-DTARGET_OS={build_platform.os}')
    cmake_args.append(
        f"-DBOOST_ROOT={cmake_path(os.path.join(install_dir, 'boost'))}")
    cmake_args.append(
        f"-DLYRA_DIR={cmake_path(os.path.join(install_dir, 'lyra'))}")
    cmake_args.append(
        f"-DWEBRTC_INCLUDE_DIR={cmake_path(webrtc_info.webrtc_include_dir)}")
    cmake_args.append(
        f"-DWEBRTC_LIBRARY_DIR={cmake_path(webrtc_info.webrtc_library_dir)}")
    cmake_args.append(
        f"-DSORA_DIR={cmake_path(os.path.join(install_dir, 'sora'))}")

    if build_platform.os == 'ubuntu':
        # クロスコンパイルの設定。
        # 本来は toolchain ファイルに書く内容
        cmake_args += [
            f"-DCMAKE_C_COMPILER={os.path.join(webrtc_info.clang_dir, 'bin', 'clang')}",
            f"-DCMAKE_CXX_COMPILER={os.path.join(webrtc_info.clang_dir, 'bin', 'clang++')}",
            f"-DLIBCXX_INCLUDE_DIR={cmake_path(os.path.join(webrtc_info.libcxx_dir, 'include'))}",
        ]
    elif build_platform.os == 'macos':
        sysroot = cmdcap(['xcrun', '--sdk', 'macosx', '--show-sdk-path'])
        cmake_args += [
            "-DCMAKE_SYSTEM_PROCESSOR=arm64",
            "-DCMAKE_OSX_ARCHITECTURES=arm64",
            "-DCMAKE_C_COMPILER=clang",
            "-DCMAKE_C_COMPILER_TARGET=aarch64-apple-darwin",
            "-DCMAKE_CXX_COMPILER=clang++",
            "-DCMAKE_CXX_COMPILER_TARGET=aarch64-apple-darwin",
            f"-DCMAKE_SYSROOT={sysroot}",
        ]

    setup(
        name="sora_sdk",
        version="2023.0.1",
        author="Shiguredo Inc.",
        author_email="contact+github@shiguredo.jp",
        description="WebRTC SFU Sora Python SDK",
        url="https://github.com/shiguredo/sora-python-sdk",
        license="Apache License 2.0",
        packages=['sora_sdk', 'sora_sdk.model_coeffs'],
        package_dir={'': 'src'},
        cmake_install_dir="src/sora_sdk",
        cmake_args=cmake_args,
        include_package_data=True,
        python_requires=">=3.8"
    )


def main():
    deps_dir = os.getenv('SORA_SDK_DEPS_DIR')
    build_platform = get_build_platform()
    if deps_dir is None:
        with tempfile.TemporaryDirectory(prefix="sora-python-sdk-") as temp_dir:
            run_setup(build_platform, temp_dir)
    else:
        run_setup(build_platform, deps_dir)


if __name__ == '__main__':
    main()

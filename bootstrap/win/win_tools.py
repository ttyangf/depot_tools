# Copyright 2016 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import collections
import contextlib
import fnmatch
import hashlib
import logging
import os
import platform
import posixpath
import shutil
import string
import subprocess
import sys
import tempfile


THIS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(THIS_DIR, '..', '..'))

DEVNULL = open(os.devnull, 'w')

BAT_EXT = '.bat' if sys.platform.startswith('win') else ''

# Top-level stubs to generate that fall through to executables within the Git
# directory.
STUBS = {
  'git.bat': 'cmd\\git.exe',
  'gitk.bat': 'cmd\\gitk.exe',
  'ssh.bat': 'usr\\bin\\ssh.exe',
  'ssh-keygen.bat': 'usr\\bin\\ssh-keygen.exe',
}


# Accumulated template parameters for generated stubs.
class Template(collections.namedtuple('Template', (
    'PYTHON_RELDIR', 'PYTHON_BIN_RELDIR', 'PYTHON_BIN_RELDIR_UNIX',
    'GIT_BIN_RELDIR', 'GIT_BIN_RELDIR_UNIX', 'GIT_PROGRAM',
    ))):

  @classmethod
  def empty(cls):
    return cls(**{k: None for k in cls._fields})

  def maybe_install(self, name, dst_path):
    """Installs template |name| to |dst_path| if it has changed.

    This loads the template |name| from THIS_DIR, resolves template parameters,
    and installs it to |dst_path|. See `maybe_update` for more information.

    Args:
      name (str): The name of the template to install.
      dst_path (str): The destination filesystem path.

    Returns (bool): True if |dst_path| was updated, False otherwise.
    """
    template_path = os.path.join(THIS_DIR, name)
    with open(template_path, 'r') as fd:
      t = string.Template(fd.read())
    return maybe_update(t.safe_substitute(self._asdict()), dst_path)


def maybe_update(content, dst_path):
  """Writes |content| to |dst_path| if |dst_path| does not already match.

  This function will ensure that there is a file at |dst_path| containing
  |content|. If |dst_path| already exists and contains |content|, no operation
  will be performed, preserving filesystem modification times and avoiding
  potential write contention.

  Args:
    content (str): The file content.
    dst_path (str): The destination filesystem path.

  Returns (bool): True if |dst_path| was updated, False otherwise.
  """
  # If the path already exists and matches the new content, refrain from writing
  # a new one.
  if os.path.exists(dst_path):
    with open(dst_path, 'r') as fd:
      if fd.read() == content:
        return False

  logging.debug('Updating %r', dst_path)
  with open(dst_path, 'w') as fd:
    fd.write(content)
  return True


def maybe_copy(src_path, dst_path):
  """Writes the content of |src_path| to |dst_path| if needed.

  See `maybe_update` for more information.

  Args:
    src_path (str): The content source filesystem path.
    dst_path (str): The destination filesystem path.

  Returns (bool): True if |dst_path| was updated, False otherwise.
  """
  with open(src_path, 'r') as fd:
    content = fd.read()
  return maybe_update(content, dst_path)


def call_if_outdated(stamp_path, stamp_version, fn):
  """Invokes |fn| if the stamp at |stamp_path| doesn't match |stamp_version|.

  This can be used to keep a filesystem record of whether an operation has been
  performed. The record is stored at |stamp_path|. To invalidate a record,
  change the value of |stamp_version|.

  After |fn| completes successfully, |stamp_path| will be updated to match
  |stamp_version|, preventing the same update from happening in the future.

  Args:
    stamp_path (str): The filesystem path of the stamp file.
    stamp_version (str): The desired stamp version.
    fn (callable): A callable to invoke if the current stamp version doesn't
        match |stamp_version|.

  Returns (bool): True if an update occurred.
  """

  stamp_version = stamp_version.strip()
  if os.path.isfile(stamp_path):
    with open(stamp_path, 'r') as fd:
      current_version = fd.read().strip()
    if current_version == stamp_version:
      return False

  fn()

  with open(stamp_path, 'w') as fd:
    fd.write(stamp_version)
  return True


def _in_use(path):
  """Checks if a Windows file is in use.

  When Windows is using an executable, it prevents other writers from
  modifying or deleting that executable. We can safely test for an in-use
  file by opening it in write mode and checking whether or not there was
  an error.

  Returns (bool): True if the file was in use, False if not.
  """
  try:
    with open(path, 'r+'):
      return False
  except IOError:
    return True


def _check_call(argv, stdin_input=None, **kwargs):
  """Wrapper for subprocess.check_call that adds logging."""
  logging.info('running %r', argv)
  if stdin_input is not None:
    kwargs['stdin'] = subprocess.PIPE
  proc = subprocess.Popen(argv, **kwargs)
  proc.communicate(input=stdin_input)
  if proc.returncode:
    raise subprocess.CalledProcessError(proc.returncode, argv, None)


def _safe_rmtree(path):
  if not os.path.exists(path):
    return

  def _make_writable_and_remove(path):
    st = os.stat(path)
    new_mode = st.st_mode | 0200
    if st.st_mode == new_mode:
      return False
    try:
      os.chmod(path, new_mode)
      os.remove(path)
      return True
    except Exception:
      return False

  def _on_error(function, path, excinfo):
    if not _make_writable_and_remove(path):
      logging.warning('Failed to %s: %s (%s)', function, path, excinfo)

  shutil.rmtree(path, onerror=_on_error)


@contextlib.contextmanager
def _tempdir():
  tdir = None
  try:
    tdir = tempfile.mkdtemp()
    yield tdir
  finally:
    _safe_rmtree(tdir)


def get_os_bitness():
  """Returns bitness of operating system as int."""
  return 64 if platform.machine().endswith('64') else 32


def get_target_git_version(args):
  """Returns git version that should be installed."""
  if args.bleeding_edge:
    git_version_file = 'git_version_bleeding_edge.txt'
  else:
    git_version_file = 'git_version.txt'
  with open(os.path.join(THIS_DIR, git_version_file)) as f:
    return f.read().strip()


def clean_up_old_git_installations(git_directory, force):
  """Removes git installations other than |git_directory|."""
  for entry in fnmatch.filter(os.listdir(ROOT_DIR), 'git-*_bin'):
    full_entry = os.path.join(ROOT_DIR, entry)
    if force or full_entry != git_directory:
      logging.info('Cleaning up old git installation %r', entry)
      _safe_rmtree(full_entry)


def clean_up_old_installations(skip_dir):
  """Removes Python installations other than |skip_dir|.

  This includes an "in-use" check against the "python.exe" in a given directory
  to avoid removing Python executables that are currently ruinning. We need
  this because our Python bootstrap may be run after (and by) other software
  that is using the bootstrapped Python!
  """
  root_contents = os.listdir(ROOT_DIR)
  for f in ('win_tools-*_bin', 'python27*_bin', 'git-*_bin'):
    for entry in fnmatch.filter(root_contents, f):
      full_entry = os.path.join(ROOT_DIR, entry)
      if full_entry == skip_dir or not os.path.isdir(full_entry):
        continue

      logging.info('Cleaning up old installation %r', entry)
      for python_exe in (
          os.path.join(full_entry, 'python', 'bin', 'python.exe'), # CIPD
          os.path.join(full_entry, 'python.exe'), # Legacy ZIP distributions.
          ):
        if os.path.isfile(python_exe) and _in_use(python_exe):
          logging.info('Python executable %r is in-use; skipping', python_exe)
          break
      else:
        _safe_rmtree(full_entry)


def cipd_ensure(args, dest_directory, package, version):
  """Installs a CIPD package using "ensure"."""
  logging.info('Installing CIPD package %r @ %r', package, version)
  manifest_text = '%s %s\n' % (package, version)

  cipd_args = [
    args.cipd_client,
    'ensure',
    '-ensure-file', '-',
    '-root', dest_directory,
  ]
  if args.cipd_cache_directory:
    cipd_args.extend(['-cache-dir', args.cipd_cache_directory])
  if args.verbose:
    cipd_args.append('-verbose')
  _check_call(cipd_args, stdin_input=manifest_text)


def need_to_install_git(args, git_directory, legacy):
  """Returns True if git needs to be installed."""
  if args.force:
    return True

  is_cipd_managed = os.path.exists(os.path.join(git_directory, '.cipd'))
  if legacy:
    if is_cipd_managed:
      # Converting from non-legacy to legacy, need reinstall.
      return True
    if not os.path.exists(os.path.join(
        git_directory, 'etc', 'profile.d', 'python.sh')):
      return True
  elif not is_cipd_managed:
    # Converting from legacy to CIPD, need reinstall.
    return True

  git_exe_path = os.path.join(git_directory, 'bin', 'git.exe')
  if not os.path.exists(git_exe_path):
    return True
  if subprocess.call(
      [git_exe_path, '--version'],
      stdout=DEVNULL, stderr=DEVNULL) != 0:
    return True

  gen_stubs = STUBS.keys()
  gen_stubs.append('git-bash')
  for stub in gen_stubs:
    full_path = os.path.join(ROOT_DIR, stub)
    if not os.path.exists(full_path):
      return True
    with open(full_path) as f:
      if os.path.relpath(git_directory, ROOT_DIR) not in f.read():
        return True

  return False


def install_git_legacy(args, git_version, git_directory, cipd_platform):
  _safe_rmtree(git_directory)
  with _tempdir() as temp_dir:
    cipd_ensure(args, temp_dir,
        package='infra/depot_tools/git_installer/%s' % cipd_platform,
        version='v' + git_version.replace('.', '_'))

    # 7-zip has weird expectations for command-line syntax. Pass it as a string
    # to avoid subprocess module quoting breaking it. Also double-escape
    # backslashes in paths.
    _check_call(' '.join([
      os.path.join(temp_dir, 'git-installer.exe'),
      '-y',
      '-InstallPath="%s"' % git_directory.replace('\\', '\\\\'),
      '-Directory="%s"' % git_directory.replace('\\', '\\\\'),
    ]))


def install_git(args, git_version, git_directory, legacy):
  """Installs |git_version| into |git_directory|."""
  # TODO: Remove legacy version once everyone is on bundled Git.
  cipd_platform = 'windows-%s' % ('amd64' if args.bits == 64 else '386')
  if legacy:
    install_git_legacy(args, git_version, git_directory, cipd_platform)
  else:
    # When migrating from legacy, we want to nuke this directory. In other
    # cases, CIPD will handle the cleanup.
    if not os.path.isdir(os.path.join(git_directory, '.cipd')):
      logging.info('Deleting legacy Git directory: %s', git_directory)
      _safe_rmtree(git_directory)

    cipd_ensure(args, git_directory,
        package='infra/git/%s' % (cipd_platform,),
        version=git_version)

  if legacy:
    # The non-legacy Git bundle includes "python.sh".
    #
    # TODO: Delete "profile.d.python.sh" after legacy mode is removed.
    shutil.copyfile(
        os.path.join(THIS_DIR, 'profile.d.python.sh'),
        os.path.join(git_directory, 'etc', 'profile.d', 'python.sh'))


def ensure_git(args, template):
  git_version = get_target_git_version(args)

  git_directory_tag = git_version.split(':')
  git_directory = os.path.join(
      ROOT_DIR, 'git-%s-%s_bin' % (git_directory_tag[-1], args.bits))

  clean_up_old_git_installations(git_directory, args.force)

  git_bin_dir = os.path.relpath(git_directory, ROOT_DIR)
  template = template._replace(
      GIT_BIN_RELDIR=git_bin_dir,
      GIT_BIN_RELDIR_UNIX=git_bin_dir)

  # Modern Git versions use CIPD tags beginning with "version:". If the tag
  # does not begin with that, use the legacy installer.
  legacy = not git_version.startswith('version:')
  if need_to_install_git(args, git_directory, legacy):
    install_git(args, git_version, git_directory, legacy)

  git_postprocess(template, git_directory)

  return template


# Version of "git_postprocess" system configuration (see |git_postprocess|).
GIT_POSTPROCESS_VERSION = '1'


def git_get_mingw_dir(git_directory):
  """Returns (str) The "mingw" directory in a Git installation, or None."""
  for candidate in ('mingw64', 'mingw32'):
    mingw_dir = os.path.join(git_directory, candidate)
    if os.path.isdir(mingw_dir):
      return mingw_dir
  return None


def git_postprocess(template, git_directory):
  # Update depot_tools files for "git help <command>"
  mingw_dir = git_get_mingw_dir(git_directory)
  if mingw_dir:
    docsrc = os.path.join(ROOT_DIR, 'man', 'html')
    git_docs_dir = os.path.join(mingw_dir, 'share', 'doc', 'git-doc')
    for name in os.listdir(docsrc):
      maybe_copy(
          os.path.join(docsrc, name),
          os.path.join(git_docs_dir, name))
  else:
    logging.info('Could not find mingw directory for %r.', git_directory)

  # Create Git templates and configure its base layout.
  for stub_name, relpath in STUBS.iteritems():
    stub_template = template._replace(GIT_PROGRAM=relpath)
    stub_template.maybe_install(
        'git.template.bat',
        os.path.join(ROOT_DIR, stub_name))

  # Set-up our system configuration environment. The following set of
  # parameters is versioned by "GIT_POSTPROCESS_VERSION". If they change,
  # update "GIT_POSTPROCESS_VERSION" accordingly.
  def configure_git_system():
    git_bat_path = os.path.join(ROOT_DIR, 'git.bat')
    _check_call([git_bat_path, 'config', '--system', 'core.autocrlf', 'false'])
    _check_call([git_bat_path, 'config', '--system', 'core.filemode', 'false'])
    _check_call([git_bat_path, 'config', '--system', 'core.preloadindex',
                 'true'])
    _check_call([git_bat_path, 'config', '--system', 'core.fscache', 'true'])

  call_if_outdated(
      os.path.join(git_directory, '.git_postprocess'),
      GIT_POSTPROCESS_VERSION,
      configure_git_system)


def main(argv):
  parser = argparse.ArgumentParser()
  parser.add_argument('--verbose', action='store_true')
  parser.add_argument('--win-tools-name',
                      help='The directory of the Python installation. '
                           '(legacy) If missing, use legacy Windows tools '
                           'processing')
  parser.add_argument('--bleeding-edge', action='store_true',
                      help='Force bleeding edge Git.')

  group = parser.add_argument_group('legacy flags')
  group.add_argument('--force', action='store_true',
                     help='Always re-install everything.')
  group.add_argument('--bits', type=int, choices=(32,64),
                     help='Bitness of the client to install. Default on this'
                     ' system: %(default)s', default=get_os_bitness())
  group.add_argument('--cipd-client',
                     help='Path to CIPD client binary. default: %(default)s',
                     default=os.path.join(ROOT_DIR, 'cipd'+BAT_EXT))
  group.add_argument('--cipd-cache-directory',
                     help='Path to CIPD cache directory.')
  args = parser.parse_args(argv)

  logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARN)

  template = Template.empty()
  if not args.win_tools_name:
    # Legacy support.
    template = template._replace(
        PYTHON_RELDIR='python276_bin',
        PYTHON_BIN_RELDIR='python276_bin',
        PYTHON_BIN_RELDIR_UNIX='python276_bin')
    template = ensure_git(args, template)
  else:
    template = template._replace(
        PYTHON_RELDIR=os.path.join(args.win_tools_name, 'python'),
        PYTHON_BIN_RELDIR=os.path.join(args.win_tools_name, 'python', 'bin'),
        PYTHON_BIN_RELDIR_UNIX=posixpath.join(
            args.win_tools_name, 'python', 'bin'),
        GIT_BIN_RELDIR=os.path.join(args.win_tools_name, 'git'),
        GIT_BIN_RELDIR_UNIX=posixpath.join(args.win_tools_name, 'git'))

    win_tools_dir = os.path.join(ROOT_DIR, args.win_tools_name)
    git_postprocess(template, os.path.join(win_tools_dir, 'git'))

    # Clean up any old Python installations.
    clean_up_old_installations(win_tools_dir)

  # Emit our Python bin depot-tools-relative directory. This is ready by
  # "python.bat" to identify the path of the current Python installation.
  #
  # We use this indirection so that upgrades can change this pointer to
  # redirect "python.bat" to a new Python installation. We can't just update
  # "python.bat" because batch file executions reload the batch file and seek
  # to the previous cursor in between every command, so changing the batch
  # file contents could invalidate any existing executions.
  #
  # The intention is that the batch file itself never needs to change when
  # switching Python versions.
  maybe_update(
      template.PYTHON_BIN_RELDIR,
      os.path.join(ROOT_DIR, 'python_bin_reldir.txt'))

  # Install our "python.bat" shim.
  # TODO: Move this to generic shim installation once legacy support is
  # removed and this code path is the only one.
  template.maybe_install(
      'python27.new.bat',
      os.path.join(ROOT_DIR, 'python.bat'))

  # Re-evaluate and regenerate our root templated files.
  for src_name, dst_name in (
      ('git-bash.template.sh', 'git-bash'),
      ('pylint.new.bat', 'pylint.bat'),
      ):
    template.maybe_install(src_name, os.path.join(ROOT_DIR, dst_name))

  return 0


if __name__ == '__main__':
  sys.exit(main(sys.argv[1:]))

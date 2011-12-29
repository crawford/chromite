#!/usr/bin/python
# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Script that syncs the repo manifest with .DEPS.git.  Designed to be run
   periodically from a host machine."""

import filecmp
import optparse
import os
import re
import shutil
import sys
import StringIO
import tempfile

# Want to use correct version of libraries even when executed through symlink.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             '..', '..'))
import chromite.bin.chrome_set_ver as chrome_set_ver
import chromite.lib.cros_build_lib as cros_lib
import chromite.buildbot.constants as constants
import chromite.buildbot.manifest_version as manifest_version
import chromite.buildbot.repository as repository

_CHROMIUM_ROOT = 'chromium'
_CHROMIUM_SRC_ROOT = os.path.join(_CHROMIUM_ROOT, 'src')
_CHROMIUM_SRC_INTERNAL = os.path.join(_CHROMIUM_ROOT, 'src-internal')
_CHROMIUM_CROS_DEPS = os.path.join(_CHROMIUM_SRC_ROOT, 'tools/cros.DEPS/DEPS')

_BEGIN_MARKER = """\
<!-- @@@@ BEGIN AUTOGENERATED BROWSER PROJECTS - DON'T MODIFY! @@@@ -->\n\n"""

_END_MARKER = """\
<!-- @@@@ END AUTOGENERATED BROWSER PROJECTS - DON'T MODIFY! @@@@ -->\n\n"""

_EXTERNAL_HEADER = """\
  <!-- Begin Chromium (browser) projects -->
  <!-- Hardcoded revision="refs/heads/master" is intentional here -->\n\n"""

_EXTERNAL_PROJECT = """\
  <project path="%(path)s"
           name="%(name)s"
           revision="refs/heads/master" />\n"""

_CROS_HEADER = """\
  <!-- Begin CrOS-specific Chromium (browser) projects -->
  <!-- Hardcoded revision="refs/heads/master" is intentional here -->\n\n"""

_INTERNAL_HEADER = """\
  <!-- Begin Chrome browser (PRIVATE) projects -->
  <!-- Hardcoded revision="refs/heads/master" is intentional here -->\n\n"""

_INTERNAL_PROJECT = """\
  <project remote="cros-internal"
           path="%(path)s"
           name="%(name)s"
           revision="refs/heads/master" />\n"""

_EXTERNAL_MANIFEST_DIR = 'update-manifest'
_INTERNAL_MANIFEST_DIR = 'update-manifest-internal'
_EXTERNAL_MANIFEST_PROJECT = 'chromiumos/manifest'
_INTERNAL_MANIFEST_PROJECT = 'chromeos/manifest-internal'
_EXTERNAL_TEST_DIR = 'external'
_INTERNAL_TEST_DIR = 'internal'

_CHROMIUM_SRC_PROJECT = 'chromium/src'
_CHROMIUM_SRC_INTERNAL_PROJECT = 'chrome/src-internal'

_TEST_MANIFEST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<manifest>
%(content)s
</manifest>"""


def ConvertDepsToManifest(deps_file, manifest_file, template, blacklist=None):
  """Convert dependencies in .DEPS.git files to manifest entries.

  Arguments:
    deps_file: The path to the .DEPS.git file.
    manifest_file: The file object to write manifest entries to.
    template: The template to use for manifest projects. One of
              (_EXTERNAL_PROJECT, _INTERNAL_PROJECT)
    blacklist: A list of projects to ignore.
  """
  _, merged_deps = chrome_set_ver.GetParsedDeps(deps_file)
  mappings = chrome_set_ver.GetPathToProjectMappings(merged_deps)

  # Check for double checkouts and blacklisted projects.
  previous_projects = set([])
  for rel_path, project in sorted(mappings.items(),
                                  key=lambda mapping: mapping[1]):
    rel_path = os.path.join('chromium', rel_path)
    if blacklist and project in blacklist:
      cros_lib.Warning('Skipping project %s in %s' % (project, deps_file))
    elif project in previous_projects:
      cros_lib.Warning('Found double checkout of %s to %s'
                       % (project, rel_path))
    else:
      manifest_file.write(template % {'path' : rel_path, 'name' : project})
      previous_projects.add(project)


class ManifestException(Exception):
  pass


def ParseManifestString(manifest_xml):
  """Returns a cros_lib.ManifestHandler object containing the parsed XML.

  Arguments:
    manifest_xml: The well-formed XML snippet to parse.

  Returns:
    A cros_build_lib.ManifestHandler object.
  """
  return cros_lib.ManifestHandler.ParseManifest(StringIO.StringIO(manifest_xml))


def CheckForNonChromeProjects(manifest_xml):
  """Make sure we didn't remove non-chrome projects from the manifest.

  Arguments:
    xml_snippet_object: The file object containing manifest entries to examine.
  """
  handler = ParseManifestString(manifest_xml)
  for project, attributes in handler.projects.iteritems():
    if not attributes.get('path', '').startswith('chromium/'):
      raise ManifestException('Project %s was about to be accidentally removed!'
                              % project)


def GetListOfProjects(manifest_xml):
  """Returns a list of projects specified in the manifest.

  Arguments:
    manifest_xml: The well-formed XML snippet to parse.

  Returns:
    A list of projects.
  """
  return ParseManifestString(manifest_xml).projects.keys()


class Manifest(object):
  """Encapsulates manifest update logic for an external or internal manifest."""
  def __init__(self, repo_root, manifest_path, testroot, internal, dryrun=True):
    self.repo_root = repo_root
    self.testroot = testroot
    self.manifest_path = manifest_path
    self.manifest_dir = os.path.dirname(manifest_path)
    self.new_manifest_path = os.path.join(self.manifest_dir,
                                          'new_update_manifest.xml')
    self.internal = internal
    self.dryrun = dryrun

  def _PartitionManifest(self):
    """Partitions the manifest around the autogenerated section.

    Returns:
      A 3-tuple containing the contents of the manifest that come before the
      auto-generated section, the autogenerated section itself, and the contents
      that come after the auto-generated section.
    """
    with open(self.manifest_path, 'r') as f:
      lines = f.read()

    regex = r'(.*)(%s)(.*)(%s)(.*)' % (
        re.escape(_BEGIN_MARKER.strip()),
        re.escape(_END_MARKER.strip()))

    result = re.match(regex, lines, re.DOTALL)
    if result:
      return result.group(1), result.group(3), result.group(5)
    else:
      raise ManifestException('Chromium projects begin/end markers not found!')

  def CreateNewManifest(self):
    """Generates a new manifest with updated Chrome entries."""
    # Prepare git repo for push
    manifest_version.PrepForChanges(self.manifest_dir, False)

    top_part, overwritten, bottom_part = self._PartitionManifest()
    CheckForNonChromeProjects(_TEST_MANIFEST_TEMPLATE
                              % {'content' : overwritten})

    with open(self.new_manifest_path, 'w') as new_manifest:
      new_manifest.write(top_part)
      new_manifest.write(_BEGIN_MARKER + _EXTERNAL_HEADER)
      new_manifest.write(_EXTERNAL_PROJECT % {'path' : 'chromium/src',
                                              'name' : 'chromium/src'})
      src_root = os.path.join(self.repo_root, _CHROMIUM_SRC_ROOT)
      ConvertDepsToManifest(os.path.join(src_root, '.DEPS.git'), new_manifest,
                            _EXTERNAL_PROJECT)
      new_manifest.write('\n')

      # Convert cros.DEPS, skipping projects already in the manifest.
      new_manifest.write(_CROS_HEADER)
      ConvertDepsToManifest(os.path.join(src_root, 'tools/cros.DEPS/DEPS'),
                            new_manifest, _EXTERNAL_PROJECT,
                            blacklist=GetListOfProjects(top_part + bottom_part))
      new_manifest.write('\n')

      if self.internal:
        new_manifest.write(_INTERNAL_HEADER)
        new_manifest.write(_INTERNAL_PROJECT
                           % {'path' : 'chromium/src-internal',
                              'name' : 'chrome/src-internal'})
        internal_deps = os.path.join(self.repo_root, _CHROMIUM_SRC_INTERNAL,
                                     '.DEPS.git')
        ConvertDepsToManifest(internal_deps, new_manifest, _INTERNAL_PROJECT)
        new_manifest.write('\n')

      new_manifest.write(_END_MARKER)
      # Strip out the empty lines at the top of the bottom block.
      new_manifest.write(re.match(r'\s*(^\s*\S+.*)', bottom_part,
                         re.S | re.M).group(1))

  def IsNewManifestDifferent(self):
    """Checks if newly generated manifest is different from old manifest."""
    return not filecmp.cmp(self.new_manifest_path, self.manifest_path,
                           shallow=False)

  def TestNewManifest(self):
    """Runs a 'repo sync' off of new manifest to verify things aren't broken."""
    # Copy to .repo/manifest.xml and run repo sync
    test_dir = os.path.join(
        self.testroot,
        _INTERNAL_TEST_DIR if self.internal else _EXTERNAL_TEST_DIR)
    if not os.path.isdir(test_dir):
      os.makedirs(test_dir)

    git_url = (constants.MANIFEST_INT_URL if self.internal
               else constants.MANIFEST_URL)
    repo = repository.RepoRepository(git_url, test_dir)
    try:
      repo.Sync(local_manifest=self.new_manifest_path, jobs=12)
    except Exception:
      cros_lib.Error('Failed to sync with new manifest!')
      raise
    finally:
      # Sync back to original manifest
      repo.Sync(local_manifest=repository.RepoRepository.DEFAULT_MANIFEST,
                jobs=12)

  def PushChanges(self):
    """Push changes to manifest live."""
    shutil.move(self.new_manifest_path, self.manifest_path)
    cros_lib.RunCommand(['git', 'add', self.manifest_path],
                        cwd=self.manifest_dir)
    cros_lib.RunCommand(['git', 'commit',
                         '-m',
                         'Auto-updating manifest to match .DEPS.git file'],
                         cwd=self.manifest_dir)
    cros_lib.GitPushWithRetry(manifest_version.PUSH_BRANCH,
                              cwd=self.manifest_dir,
                              dryrun=self.dryrun)

  def PerformUpdate(self):
    try:
      self.CreateNewManifest()
      if self.IsNewManifestDifferent():
        self.TestNewManifest()
        self.PushChanges()
    except ManifestException:
      cros_lib.Error('Errors encountered while updating manifest!')
      raise


def GetSource(repo_path, project_name, internal=False):
  """Checks out and updates a gerrit project.

  Arguments:
    repo_path: Absolute path of checkout.
    project_name: Name of Gerrit project to pull down.
    internal: Whether the project is an internal project.
  """
  if not os.path.isdir(repo_path):
    if internal:
      project_url = os.path.join(constants.GERRIT_INT_SSH_URL, project_name)
    else:
      project_url = os.path.join(constants.GERRIT_SSH_URL, project_name)

    cros_lib.RunCommand(['git', 'clone', project_url, repo_path])

  cros_lib.RunCommand(['git', 'pull', '--ff-only'], cwd=repo_path)


def _CheckTestRootOption(_option, _opt_str, value, parser):
  """Validate and convert buildroot to full-path form."""
  value = value.strip()
  if not value or value == '/':
    raise optparse.OptionValueError('Invalid buildroot specified')

  parser.values.testroot = os.path.realpath(os.path.expanduser(value))


def main(argv=None):
  if argv is None:
    argv = sys.argv[1:]

  usage = 'usage: %prog'
  parser = optparse.OptionParser(usage=usage)

  parser.add_option('-r', '--testroot', action='callback', dest='testroot',
                    type='string', callback=_CheckTestRootOption,
                    help=('Directory where test checkout is stored.'))
  parser.add_option('-f', '--force', default=False, action='store_true',
                    help='Actually push manifest changes.')
  parser.add_option('-v', '--verbose', default=False, action='store_true',
                    help='Run with debug output.')
  (options, _inputs) = parser.parse_args(argv)

  if not options.testroot:
    cros_lib.Die('Please specify a test root!')

  # Set cros_build_lib debug level to hide RunCommand spew.
  if options.verbose:
    cros_lib.DebugLevel.SetDebugLevel(cros_lib.DebugLevel.DEBUG)
  else:
    cros_lib.DebugLevel.SetDebugLevel(cros_lib.DebugLevel.WARNING)

  repo_root = cros_lib.FindRepoCheckoutRoot()
  chromium_src_root = os.path.join(repo_root, _CHROMIUM_SRC_ROOT)
  if not os.path.isdir(chromium_src_root):
    cros_lib.Die('chromium src/ dir not found')

  external_manifest_dir = os.path.join(tempfile.gettempdir(),
                                       _EXTERNAL_MANIFEST_DIR)
  internal_manifest_dir = os.path.join(tempfile.gettempdir(),
                                       _INTERNAL_MANIFEST_DIR)

  # Sync manifest and .DEPS.git files
  GetSource(external_manifest_dir, _EXTERNAL_MANIFEST_PROJECT)
  GetSource(internal_manifest_dir, _INTERNAL_MANIFEST_PROJECT,
            internal=True)

  project_list = [_CHROMIUM_SRC_PROJECT, _CHROMIUM_SRC_INTERNAL_PROJECT]
  cros_lib.RunCommand(['repo', 'sync'] + project_list, cwd=repo_root)

  # Update external manifest
  Manifest(repo_root, os.path.join(external_manifest_dir, 'oldlayout.xml'),
           options.testroot, internal=False,
           dryrun=not options.force).PerformUpdate()

  # Update internal manifest
  Manifest(repo_root, os.path.join(internal_manifest_dir, 'oldlayout.xml'),
           options.testroot, internal=True,
           dryrun=not options.force).PerformUpdate()


if __name__ == '__main__':
  main()

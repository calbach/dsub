# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utility functions and classes for input, output, and script parameters."""

import argparse
import collections
import csv
import datetime
import os
import re

import dsub_util

AUTO_PREFIX_INPUT = 'INPUT_'  # Prefix for auto-generated input names
AUTO_PREFIX_OUTPUT = 'OUTPUT_'  # Prefix for auto-generated output names

P_LOCAL = 'local'
P_GCS = 'google-cloud-storage'
FILE_PROVIDERS = frozenset([P_LOCAL, P_GCS])

RESERVED_LABELS = frozenset(
    ['job-name', 'job-id', 'user-id', 'task-id', 'dsub-version'])


def validate_param_name(name, param_type):
  """Validate that the name follows posix conventions for env variables."""
  # http://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap03.html#tag_03_235
  #
  # 3.235 Name
  # In the shell command language, a word consisting solely of underscores,
  # digits, and alphabetics from the portable character set.
  if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
    raise ValueError('Invalid %s: %s' % (param_type, name))


class ListParamAction(argparse.Action):
  """Append each value as a separate element to the parser destination.

  This class satisifes the action interface of argparse.ArgumentParser and
  refines the 'append' action for arguments with `nargs='*'`.

  For the parameters:

    --myarg val1 val2 --myarg val3

  The 'append' action yields:

    args.myval = ['val1 val2', 'val3']

  While ListParamAction yields:

    args.myval = ['val1', 'val2', 'val3']
  """

  def __init__(self, option_strings, dest, **kwargs):
    super(ListParamAction, self).__init__(option_strings, dest, **kwargs)

  def __call__(self, parser, namespace, values, option_string=None):
    params = getattr(namespace, self.dest, [])

    # Input comes in as a list (possibly len=1) of NAME=VALUE pairs
    for arg in values:
      params.append(arg)
    setattr(namespace, self.dest, params)


class UriParts(str):
  """Subclass string for multipart URIs.

  This string subclass is used for URI references. The path and basename
  attributes are used to maintain separation of this information in cases where
  it might otherwise be ambiguous. The value of a UriParts string is a URI.

  Attributes:
    path: Strictly speaking, the path attribute is the entire leading part of
      a URI (including scheme, host, and path). This attribute defines the
      hierarchical location of a resource. Path must end in a forward
      slash. Local file URIs are represented as relative URIs (path only).
    basename: The last token of a path that follows a forward slash. Generally
      this defines a specific resource or a pattern that matches resources. In
      the case of URI's that consist only of a path, this will be empty.

  Examples:
    | uri                         |  uri.path              | uri.basename  |
    +-----------------------------+------------------------+---------------|
    | gs://bucket/folder/file.txt | 'gs://bucket/folder/'  | 'file.txt'    |
    | http://example.com/1.htm    | 'http://example.com/'  | '1.htm'       |
    | /tmp/tempdir1/              | '/tmp/tempdir1/'       | ''            |
    | /tmp/ab.txt                 | '/tmp/'                | 'ab.txt'      |
  """

  def __new__(cls, path, basename):
    basename = basename if basename is not None else ''
    newuri = str.__new__(cls, path + basename)
    newuri.path = path
    newuri.basename = basename
    return newuri


class EnvParam(collections.namedtuple('EnvParam', ['name', 'value'])):
  """Name/value input parameter to a pipeline.

  Attributes:
    name (str): the input parameter and environment variable name.
    value (str): the variable value (optional).
  """
  __slots__ = ()

  def __new__(cls, name, value=None):
    validate_param_name(name, 'Environment variable')
    return super(EnvParam, cls).__new__(cls, name, value)


class LoggingParam(
    collections.namedtuple('LoggingParam', ['uri', 'file_provider'])):
  """File parameter used for logging.

  Attributes:
    uri (UriParts): A uri or local file path.
    file_provider (enum): Service or infrastructure hosting the file.
  """
  pass


class LabelParam(collections.namedtuple('LabelParam', ['name', 'value'])):
  """Name/value label parameter to a pipeline.

  Subclasses of LabelParam may flip the _allow_reserved_keys attribute in order
  to allow reserved label values to be used. The check against reserved keys
  ensures that providers can rely on the label system to track dsub-related
  values without allowing users to accidentially overwrite the labels.

  Attributes:
    name (str): the label name.
    value (str): the label value (optional).
  """
  _allow_reserved_keys = False
  __slots__ = ()

  def __new__(cls, name, value=None):
    cls.validate_label(name, value)
    return super(LabelParam, cls).__new__(cls, name, value)

  @classmethod
  def validate_label(cls, name, value):
    """Raise ValueError if the label is invalid."""
    # Rules for labels are described in:
    #  https://cloud.google.com/compute/docs/labeling-resources#restrictions

    # * Keys and values cannot be longer than 63 characters each.
    # * Keys and values can only contain lowercase letters, numeric characters,
    #   underscores, and dashes.
    # * International characters are allowed.
    # * Label keys must start with a lowercase letter and international
    #   characters are allowed.
    # * Label keys cannot be empty.
    cls._check_label_rule(name, 'name')

    # The value can be empty.
    # If not empty, must conform to the same rules as the name.
    if value:
      cls._check_label_rule(value, 'value')

    # Ensure that reserved labels are not being used.
    if not cls._allow_reserved_keys and name in RESERVED_LABELS:
      raise ValueError('Label flag (%s=...) must not use reserved keys: %r' % (
          name, list(RESERVED_LABELS)))

  @staticmethod
  def _check_label_rule(param_value, param_type):
    if len(param_value) < 1 or len(param_value) > 63:
      raise ValueError('Label %s must be 1-63 characters long: "%s"' %
                       (param_type, param_value))
    if not re.match(r'^[a-z]([-_a-z0-9]*)?$', param_value):
      raise ValueError(
          'Invalid %s for label: "%s". Must start with a lowercase letter and '
          'contain only lowercase letters, numeric characters, underscores, '
          'and dashes.' % (param_type, param_value))


class FileParam(
    collections.namedtuple('FileParam', [
        'name',
        'value',
        'docker_path',
        'uri',
        'recursive',
        'file_provider',
    ])):
  """File parameter to be automatically localized or de-localized.

  Input files are automatically localized to the pipeline VM's local disk.

  Output files are automatically de-localized to a remote URI from the
  pipeline VM's local disk.

  Attributes:
    name (str): the parameter and environment variable name.
    value (str): the original value given by the user on the command line or
                 in the TSV file.
    docker_path (str): the on-VM location; also set as the environment variable
                       value.
    uri (UriParts): A uri or local file path.
    recursive (bool): Whether recursive copy is wanted.
    file_provider (enum): Service or infrastructure hosting the file.
  """
  __slots__ = ()

  def __new__(cls,
              name,
              value=None,
              docker_path=None,
              uri=None,
              recursive=False,
              file_provider=None):
    return super(FileParam, cls).__new__(cls, name, value, docker_path, uri,
                                         recursive, file_provider)


class InputFileParam(FileParam):
  """Simple typed-derivative of a FileParam."""

  def __new__(cls,
              name,
              value=None,
              docker_path=None,
              uri=None,
              recursive=False,
              file_provider=None):
    validate_param_name(name, 'Input parameter')
    return super(InputFileParam, cls).__new__(cls, name, value, docker_path,
                                              uri, recursive, file_provider)


class OutputFileParam(FileParam):
  """Simple typed-derivative of a FileParam."""

  def __new__(cls,
              name,
              value=None,
              docker_path=None,
              uri=None,
              recursive=False,
              file_provider=None):
    validate_param_name(name, 'Output parameter')
    return super(OutputFileParam, cls).__new__(cls, name, value, docker_path,
                                               uri, recursive, file_provider)


class FileParamUtil(object):
  """Base class helper for producing FileParams from args or a tasks file.

  InputFileParams and OutputFileParams can be produced from either arguments
  passed on the command-line or as a combination of the definition in the tasks
  file header plus cell values in task records.

  This class encapsulates the generation of the FileParam name, if none is
  specified (get_variable_name()) as well as common path validation for
  input and output arguments (validate_paths).
  """

  def __init__(self, auto_prefix, relative_path):
    self.param_class = FileParam
    self._auto_prefix = auto_prefix
    self._auto_index = 0
    self._relative_path = relative_path

  def get_variable_name(self, name):
    """Produce a default variable name if none is specified."""
    if not name:
      name = '%s%s' % (self._auto_prefix, self._auto_index)
      self._auto_index += 1
    return name

  def rewrite_uris(self, raw_uri, file_provider):
    """Accept a raw uri and return rewritten versions.

    This function returns a normalized URI and a docker path. The normalized
    URI may have minor alterations meant to disambiguate and prepare for use
    by shell utilities that may require a specific format.

    The docker rewriter makes substantial modifications to the raw URI when
    constructing a docker path, but modifications must follow these rules:
      1) System specific characters are not allowed (ex. indirect paths).
      2) The path, if it is a directory, must end in a forward slash.
      3) The path will begin with the value set in self._relative_path.
      4) The path will have an additional prefix (after self._relative_path) set
         by the file provider-specific rewriter.

    Rewrite output for the docker path:
      >>> out_util = FileParamUtil('AUTO_', 'output')
      >>> out_util.rewrite_uris('gs://mybucket/myfile.txt', P_GCS)[1]
      'output/gs/mybucket/myfile.txt'
      >>> out_util.rewrite_uris('./data/myfolder/', P_LOCAL)[1]
      'output/file/data/myfolder/'

    When normalizing the URI for cloud buckets, no rewrites are done. For local
    files, the user directory will be expanded and relative paths will be
    converted to absolute:
      >>> in_util = FileParamUtil('AUTO_', 'input')
      >>> in_util.rewrite_uris('gs://mybucket/gcs_dir/', P_GCS)[0]
      'gs://mybucket/gcs_dir/'
      >>> in_util.rewrite_uris('/data/./dir_a/../myfile.txt', P_LOCAL)[0]
      '/data/myfile.txt'
      >>> in_util.rewrite_uris('file:///tmp/data/*.bam', P_LOCAL)[0]
      '/tmp/data/*.bam'

    Args:
      raw_uri: (str) the path component of the raw URI.
      file_provider: a valid provider (contained in FILE_PROVIDERS).

    Returns:
      normalized: a cleaned version of the uri provided by command line.
      docker_path: the uri rewritten in the format required for mounting inside
                   a docker worker.

    Raises:
      ValueError: if file_provider is not valid.
    """
    if file_provider == P_GCS:
      normalized, docker_path = self._gcs_uri_rewriter(raw_uri)
    elif file_provider == P_LOCAL:
      normalized, docker_path = self._local_uri_rewriter(raw_uri)
    else:
      raise ValueError('File provider not supported: %r' % file_provider)
    return normalized, os.path.join(self._relative_path, docker_path)

  @staticmethod
  def _local_uri_rewriter(raw_uri):
    """Rewrite local file URIs as required by the rewrite_uris method.

    Local file paths, unlike GCS paths, may have their raw URI simplified by
    os.path.normpath which collapses extraneous indirect characters.

    >>> FileParamUtil._local_uri_rewriter('/tmp/a_path/../B_PATH/file.txt')
    ('/tmp/B_PATH/file.txt', 'file/tmp/B_PATH/file.txt')
    >>> FileParamUtil._local_uri_rewriter('/myhome/./mydir/')
    ('/myhome/mydir/', 'file/myhome/mydir/')

    The local path rewriter will also work to preserve relative paths even
    when creating the docker path. This prevents leaking of information on the
    invoker's system to the remote system. Doing this requires a number of path
    substitutions denoted with the _<rewrite>_ convention.

    >>> FileParamUtil._local_uri_rewriter('./../upper_dir/')[1]
    'file/_dotdot_/upper_dir/'
    >>> FileParamUtil._local_uri_rewriter('~/localdata/*.bam')[1]
    'file/_home_/localdata/*.bam'

    Args:
      raw_uri: (str) the raw file or directory path.

    Returns:
      normalized: a simplified and/or expanded version of the uri.
      docker_path: the uri rewritten in the format required for mounting inside
                   a docker worker.

    """
    # The path is split into components so that the filename is not rewritten.
    raw_path, filename = os.path.split(raw_uri)
    # Generate the local path that can be resolved by filesystem operations,
    # this removes special shell characters, condenses indirects and replaces
    # any unnecessary prefix.
    prefix_replacements = [('file:///', '/'), ('~/', os.getenv('HOME')),
                           ('./', ''), ('file:/', '/')]
    normed_path = raw_path
    for prefix, replacement in prefix_replacements:
      if normed_path.startswith(prefix):
        normed_path = os.path.join(replacement, normed_path[len(prefix):])
    # Because abspath strips the trailing '/' from bare directory references
    # other than root, this ensures that all directory references end with '/'.
    normed_uri = directory_fmt(os.path.abspath(normed_path))
    normed_uri = os.path.join(normed_uri, filename)

    # Generate the path used inside the docker image;
    #  1) Get rid of extra indirects: /this/./that -> /this/that
    #  2) Rewrite required indirects as synthetic characters.
    #  3) Strip relative or absolute path leading character.
    #  4) Add 'file/' prefix.
    docker_rewrites = [(r'/\.\.', '/_dotdot_'), (r'^\.\.', '_dotdot_'),
                       (r'^~/', '_home_/'), (r'^file:/', '')]
    docker_path = os.path.normpath(raw_path)
    for pattern, replacement in docker_rewrites:
      docker_path = re.sub(pattern, replacement, docker_path)
    docker_path = docker_path.lstrip('./')  # Strips any of '.' './' '/'.
    docker_path = directory_fmt('file/' + docker_path) + filename
    return normed_uri, docker_path

  @staticmethod
  def _gcs_uri_rewriter(raw_uri):
    """Rewrite GCS file paths as required by the rewrite_uris method.

    The GCS rewriter performs no operations on the raw_path and simply returns
    it as the normalized URI. The docker path has the gs:// prefix replaced
    with gs/ so that it can be mounted inside a docker image.

    Args:
      raw_uri: (str) the raw GCS URI, prefix, or pattern.

    Returns:
      normalized: a cleaned version of the uri provided by command line.
      docker_path: the uri rewritten in the format required for mounting inside
                   a docker worker.
    """
    docker_path = raw_uri.replace('gs://', 'gs/', 1)
    return raw_uri, docker_path

  @staticmethod
  def parse_file_provider(uri):
    """Find the file provider for a URI."""
    providers = {'gs': P_GCS, 'file': P_LOCAL}
    # URI scheme detector uses a range up to 30 since none of the IANA
    # registered schemes are longer than this.
    provider_found = re.match(r'^([A-Za-z][A-Za-z0-9+.-]{0,29})://', uri)
    if provider_found:
      prefix = provider_found.group(1).lower()
    else:
      # If no provider is specified in the URI, assume that the local
      # filesystem is being used. Availability and validity of the local
      # file/directory will be checked later.
      prefix = 'file'
    if prefix in providers:
      return providers[prefix]
    else:
      raise ValueError('File prefix not supported: %s://' % prefix)

  @staticmethod
  def _validate_paths_or_fail(uri, recursive):
    """Do basic validation of the uri, return the path and filename."""
    path, filename = os.path.split(uri)

    # dsub could support character ranges ([0-9]) with some more work, but for
    # now we assume that basic asterisk wildcards are sufficient. Reject any URI
    # that includes square brackets or question marks, since we know that
    # if they actually worked, it would be accidental.
    if '[' in uri or ']' in uri:
      raise ValueError(
          'Square bracket (character ranges) are not supported: %s' % uri)
    if '?' in uri:
      raise ValueError('Question mark wildcards are not supported: %s' % uri)

    # Only support file URIs and *filename* wildcards
    # Wildcards at the directory level or "**" syntax would require better
    # support from the Pipelines API *or* doing expansion here and
    # (potentially) producing a series of FileParams, instead of one.
    if '*' in path:
      raise ValueError(
          'Path wildcard (*) are only supported for files: %s' % uri)
    if '**' in filename:
      raise ValueError('Recursive wildcards ("**") not supported: %s' % uri)
    if filename in ('..', '.'):
      raise ValueError('Path characters ".." and "." not supported '
                       'for file names: %s' % uri)

    # Do not allow non-recurssive IO to reference directories.
    if not recursive and not filename:
      raise ValueError('Input or output values that are not recursive must '
                       'reference a filename or wildcard: %s' % uri)

  def parse_uri(self, raw_uri, recursive):
    """Return a valid docker_path, uri, and file provider from a flag value."""
    # Assume recursive URIs are directory paths.
    if recursive:
      raw_uri = directory_fmt(raw_uri)
    # Get the file provider, validate the raw URI, and rewrite the path
    # component of the URI for docker and remote.
    file_provider = self.parse_file_provider(raw_uri)
    self._validate_paths_or_fail(raw_uri, recursive)
    uri, docker_uri = self.rewrite_uris(raw_uri, file_provider)
    uri_parts = UriParts(
        directory_fmt(os.path.dirname(uri)), os.path.basename(uri))
    return docker_uri, uri_parts, file_provider

  def make_param(self, name, raw_uri, recursive):
    """Return a *FileParam given an input uri."""
    docker_path, uri_parts, provider = self.parse_uri(raw_uri, recursive)
    return self.param_class(name, raw_uri, docker_path, uri_parts, recursive,
                            provider)


class InputFileParamUtil(FileParamUtil):
  """Implementation of FileParamUtil for input files."""

  def __init__(self, docker_path):
    super(InputFileParamUtil, self).__init__(AUTO_PREFIX_INPUT, docker_path)
    self.param_class = InputFileParam


class OutputFileParamUtil(FileParamUtil):
  """Implementation of FileParamUtil for output files."""

  def __init__(self, docker_path):
    super(OutputFileParamUtil, self).__init__(AUTO_PREFIX_OUTPUT, docker_path)
    self.param_class = OutputFileParam


def build_logging_param(logging_uri, util_class=OutputFileParamUtil):
  """Convenience function simplifies construction of the logging uri."""
  if not logging_uri:
    return LoggingParam(None, None)
  recursive = not logging_uri.endswith('.log')
  oututil = util_class('')
  _, uri, provider = oututil.parse_uri(logging_uri, recursive)
  if '*' in uri.basename:
    raise ValueError('Wildcards not allowed in logging URI: %s' % uri)
  return LoggingParam(uri, provider)


def split_pair(pair_string, separator, nullable_idx=1):
  """Split a string into a pair, which can have one empty value.

  Args:
    pair_string: The string to be split.
    separator: The separator to be used for splitting.
    nullable_idx: The location to be set to null if the separator is not in the
                  input string. Should be either 0 or 1.

  Returns:
    A list containing the pair.

  Raises:
    IndexError: If nullable_idx is not 0 or 1.
  """

  pair = pair_string.split(separator, 1)
  if len(pair) == 1:
    if nullable_idx == 0:
      return [None, pair[0]]
    elif nullable_idx == 1:
      return [pair[0], None]
    else:
      raise IndexError('nullable_idx should be either 0 or 1.')
  else:
    return pair


def parse_tasks_file_header(header, input_file_param_util,
                            output_file_param_util):
  """Parse the header from the tasks file into env, input, output definitions.

  Elements are formatted similar to their equivalent command-line arguments,
  but with associated values coming from the data rows.

  Environment variables columns are headered as "--env <name>"
  Inputs columns are headered as "--input <name>" with the name optional.
  Outputs columns are headered as "--output <name>" with the name optional.

  For historical reasons, bareword column headers (such as "JOB_ID") are
  equivalent to "--env var_name".

  Args:
    header: Array of header fields
    input_file_param_util: Utility for producing InputFileParam objects.
    output_file_param_util: Utility for producing OutputFileParam objects.

  Returns:
    job_params: A list of EnvParams and FileParams for the environment
    variables, LabelParams, input file parameters, and output file parameters.

  Raises:
    ValueError: If a header contains a ":" and the prefix is not supported.
  """
  job_params = []

  for col in header:

    # Reserve the "-" and "--" namespace.
    # If the column has no leading "-", treat it as an environment variable
    col_type = '--env'
    col_value = col
    if col.startswith('-'):
      col_type, col_value = split_pair(col, ' ', 1)

    if col_type == '--env':
      job_params.append(EnvParam(col_value))

    elif col_type == '--label':
      job_params.append(LabelParam(col_value))

    elif col_type == '--input' or col_type == '--input-recursive':
      name = input_file_param_util.get_variable_name(col_value)
      job_params.append(
          InputFileParam(
              name,
              recursive=(col_type.endswith('recursive')),
              file_provider=P_GCS))

    elif col_type == '--output' or col_type == '--output-recursive':
      name = output_file_param_util.get_variable_name(col_value)
      job_params.append(
          OutputFileParam(
              name,
              recursive=(col_type.endswith('recursive')),
              file_provider=P_GCS))

    else:
      raise ValueError('Unrecognized column header: %s' % col)

  return job_params


def tasks_file_to_job_data(tasks, input_file_param_util,
                           output_file_param_util):
  """Parses task parameters from a TSV.

  Args:
    tasks: Dict containing the path to a TSV file and task numbers to run
    variables, input, and output parameters as column headings. Subsequent
    lines specify parameter values, one row per job.
    input_file_param_util: Utility for producing InputFileParam objects.
    output_file_param_util: Utility for producing OutputFileParam objects.

  Returns:
    job_data: an array of records, each containing a dictionary of
    'envs', 'inputs', 'outputs', 'labels' that defines the set of parameters
    and data for each job.

  Raises:
    ValueError: If no job records were provided
  """
  job_data = []

  path = tasks['path']
  task_min = tasks.get('min')
  task_max = tasks.get('max')

  # Load the file and set up a Reader that tokenizes the fields
  param_file = dsub_util.load_file(path)
  reader = csv.reader(param_file, delimiter='\t')

  # Read the first line and extract the parameters
  header = reader.next()
  job_params = parse_tasks_file_header(header, input_file_param_util,
                                       output_file_param_util)

  # Build a list of records from the parsed input file
  for row in reader:
    # Tasks are numbered starting at 1 and since the first line of the TSV
    # file is a header, the first task appears on line 2.
    task_id = reader.line_num - 1
    if task_min and task_id < task_min:
      continue
    if task_max and task_id > task_max:
      continue

    if len(row) != len(job_params):
      dsub_util.print_error('Unexpected number of fields %s vs %s: line %s' %
                            (len(row), len(job_params), reader.line_num))

    # Each row can contain "envs", "inputs", "outputs"
    envs = []
    inputs = []
    outputs = []
    labels = []

    for i in range(0, len(job_params)):
      param = job_params[i]
      name = param.name
      if isinstance(param, EnvParam):
        envs.append(EnvParam(name, row[i]))

      elif isinstance(param, LabelParam):
        labels.append(LabelParam(name, row[i]))

      elif isinstance(param, InputFileParam):
        inputs.append(
            input_file_param_util.make_param(name, row[i], param.recursive))

      elif isinstance(param, OutputFileParam):
        outputs.append(
            output_file_param_util.make_param(name, row[i], param.recursive))

    job_data.append({
        'task-id': task_id,
        'labels': labels,
        'envs': envs,
        'inputs': inputs,
        'outputs': outputs
    })

  # Ensure that there are jobs to execute (and not just a header)
  if not job_data:
    raise ValueError('No tasks added from %s' % path)

  return job_data


def parse_pair_args(labels, argclass):
  """Parse flags of key=value pairs and return a list of argclass.

  For pair variables, we need to:
     * split the input into name=value pairs (value optional)
     * Create the EnvParam object

  Args:
    labels: list of 'key' or 'key=value' strings.
    argclass: Container class for args, must instantiate with argclass(k, v).

  Returns:
    list of argclass objects.
  """
  label_data = []
  for arg in labels:
    name, value = split_pair(arg, '=', nullable_idx=1)
    label_data.append(argclass(name, value))
  return label_data


def args_to_job_data(envs, labels, inputs, inputs_recursive, outputs,
                     outputs_recursive, input_file_param_util,
                     output_file_param_util):
  """Parse env, input, and output parameters into a job parameters and data.

  Passing arguments on the command-line allows for launching a single job.
  The env, input, and output arguments encode both the definition of the
  job as well as the single job's values.

  Env arguments are simple name=value pairs.
  Input and output file arguments can contain name=value pairs or just values.
  Either of the following is valid:

    uri
    myfile=uri

  Args:
    envs: list of environment variable job parameters
    labels: list of labels to attach to the tasks
    inputs: list of file input parameters
    inputs_recursive: list of recursive directory input parameters
    outputs: list of file output parameters
    outputs_recursive: list of recursive directory output parameters
    input_file_param_util: Utility for producing InputFileParam objects.
    output_file_param_util: Utility for producing OutputFileParam objects.

  Returns:
    job_data: an array of length one, containing a dictionary of
    'envs', 'inputs', and 'outputs' that defines the set of parameters and data
    for a job.
  """
  # Parse environmental vairables and labels.
  env_data = parse_pair_args(envs, EnvParam)
  label_data = parse_pair_args(labels, LabelParam)

  # For input files, we need to:
  #   * split the input into name=uri pairs (name optional)
  #   * get the environmental variable name, or automatically set if null.
  #   * create the input file param
  input_data = []
  for (recursive, args) in ((False, inputs), (True, inputs_recursive)):
    for arg in args:
      name, value = split_pair(arg, '=', nullable_idx=0)
      name = input_file_param_util.get_variable_name(name)
      input_data.append(
          input_file_param_util.make_param(name, value, recursive))

  # For output files, we need to:
  #   * split the input into name=uri pairs (name optional)
  #   * get the environmental variable name, or automatically set if null.
  #   * create the output file param
  output_data = []
  for (recursive, args) in ((False, outputs), (True, outputs_recursive)):
    for arg in args:
      name, value = split_pair(arg, '=', 0)
      name = output_file_param_util.get_variable_name(name)
      output_data.append(
          output_file_param_util.make_param(name, value, recursive))

  return [{
      'envs': env_data,
      'inputs': input_data,
      'outputs': output_data,
      'labels': label_data,
  }]


def validate_submit_args_or_fail(job_resources, all_task_data, provider_name,
                                 input_providers, output_providers,
                                 logging_providers):
  """Validate that arguments passed to submit_job have valid file providers.

  This utility function takes resources and task data args from `submit_job`
  in the base provider. This function will fail with a value error if any of the
  parameters are not valid. See the following example;

  >>> res = type('', (object,),
  ...            {"logging": LoggingParam('gs://logtemp', P_GCS)})()
  >>> task_data = [
  ...    {'inputs': [FileParam('IN', uri='gs://in/*', file_provider=P_GCS)]},
  ...    {'outputs': [FileParam('OUT', uri='gs://out/*', file_provider=P_GCS)]}]
  ...
  >>> validate_submit_args_or_fail(job_resources=res,
  ...                              all_task_data=task_data,
  ...                              provider_name='MYPROVIDER',
  ...                              input_providers=[P_GCS],
  ...                              output_providers=[P_GCS],
  ...                              logging_providers=[P_GCS])
  ...
  >>> validate_submit_args_or_fail(job_resources=res,
  ...                              all_task_data=task_data,
  ...                              provider_name='MYPROVIDER',
  ...                              input_providers=[P_GCS],
  ...                              output_providers=[P_LOCAL],
  ...                              logging_providers=[P_GCS])
  Traceback (most recent call last):
       ...
  ValueError: Unsupported output path (gs://out/*) for provider 'MYPROVIDER'.

  Args:
    job_resources: instance of job_util.JobResources.
    all_task_data: ([]dicts) the task data list to be validated.
    provider_name: (str) the name of the execution provider.
    input_providers: (string collection) whitelist of file providers for input.
    output_providers: (string collection) whitelist of providers for output.
    logging_providers: (string collection) whitelist of providers for logging.

  Raises:
    ValueError: if any file providers do not match the whitelists.
  """
  error_message = ('Unsupported {argname} path ({path}) for '
                   'provider {provider!r}.')
  # Validate logging file provider.
  logging = job_resources.logging
  if logging.file_provider not in logging_providers:
    raise ValueError(
        error_message.format(
            argname='logging', path=logging.uri, provider=provider_name))

  # Validate input file provider.
  for task in all_task_data:
    for argtype, whitelist in [('inputs', input_providers), ('outputs',
                                                             output_providers)]:
      argname = argtype.rstrip('s')
      for fileparam in task.get(argtype, []):

        if fileparam.file_provider not in whitelist:
          raise ValueError(
              error_message.format(
                  argname=argname, path=fileparam.uri, provider=provider_name))


def directory_fmt(directory):
  """In ensure that directories end with '/'.

  Frequently we need to ensure that directory paths end with a forward slash.
  Pythons dirname and split functions in the path library treat this
  inconsistently creating this requirement. This function is simple but was
  written to centralize documentation of an often used (and often explained)
  requirement in this codebase.

  >>> os.path.dirname('gs://bucket/folder/file.txt')
  'gs://bucket/folder'
  >>> directory_fmt(os.path.dirname('gs://bucket/folder/file.txt'))
  'gs://bucket/folder/'
  >>> os.path.dirname('/newfile')
  '/'
  >>> directory_fmt(os.path.dirname('/newfile'))
  '/'

  Specifically we need this since copy commands must know whether the
  destination is a directory to function properly. See the following shell
  interaction for an example of the inconsistency. Notice that text files are
  copied as expected but the bam is copied over the directory name.

  Multiple files copy, works as intended in all cases:
      $ touch a.txt b.txt
      $ gsutil cp ./*.txt gs://mybucket/text_dest
      $ gsutil ls gs://mybucket/text_dest/
            0  2017-07-19T21:44:36Z  gs://mybucket/text_dest/a.txt
            0  2017-07-19T21:44:36Z  gs://mybucket/text_dest/b.txt
      TOTAL: 2 objects, 0 bytes (0 B)

  Single file copy fails to copy into a directory:
      $ touch 1.bam
      $ gsutil cp ./*.bam gs://mybucket/bad_dest
      $ gsutil ls gs://mybucket/bad_dest
               0  2017-07-19T21:46:16Z  gs://mybucket/bad_dest
      TOTAL: 1 objects, 0 bytes (0 B)

  Adding a trailing forward slash fixes this:
      $ touch my.sam
      $ gsutil cp ./*.sam gs://mybucket/good_folder
      $ gsutil ls gs://mybucket/good_folder
               0  2017-07-19T21:46:16Z  gs://mybucket/good_folder/my.sam
      TOTAL: 1 objects, 0 bytes (0 B)

  Args:
    directory (str): a uri without an blob or file basename.

  Returns:
    the directory with a trailing slash.
  """
  return directory.rstrip('/') + '/'


def age_to_create_time(age, from_time=datetime.datetime.utcnow()):
  """Compute the create time (UTC) for the list filter.

  If the age is an integer value it is treated as a UTC date.
  Otherwise the value must be of the form "<integer><unit>" where supported
  units are s, m, h, d, w (seconds, months, hours, days, weeks).

  Args:
    age: A "<integer><unit>" string or integer value.
    from_time:

  Returns:
    A date value in UTC or None if age parameter is empty.
  """

  if not age:
    return None

  try:
    last_char = age[-1]

    if last_char in 'smhdw':
      if last_char == 's':
        interval = datetime.timedelta(seconds=int(age[:-1]))
      elif last_char == 'm':
        interval = datetime.timedelta(minutes=int(age[:-1]))
      elif last_char == 'h':
        interval = datetime.timedelta(hours=int(age[:-1]))
      elif last_char == 'd':
        interval = datetime.timedelta(days=int(age[:-1]))
      elif last_char == 'w':
        interval = datetime.timedelta(weeks=int(age[:-1]))

      start = from_time - interval
      epoch = datetime.datetime.utcfromtimestamp(0)

      return int((start - epoch).total_seconds())
    else:
      return int(age)

  except (ValueError, OverflowError) as e:
    raise ValueError('Unable to parse age string %s: %s' % (age, e))

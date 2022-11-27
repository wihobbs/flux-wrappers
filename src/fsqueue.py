#!/bin/env python3
###############################################################
# Copyright 2020 Lawrence Livermore National Security, LLC
# (c.f. NOTICE.LLNS)
#
# SPDX-License-Identifier: LGPL-3.0
###############################################################

import argparse
import flux
import flux.job
import flux.hostlist
import re
import sys


class CustomHelpFormatter(argparse.HelpFormatter):
    """
    Create minimal argparse format to mimic that of Slurm.

    See https://stackoverflow.com/a/31124505 for original answer to shortening
    argparse's usage documentation.
    """

    def __init__(self, prog):
        super().__init__(prog, max_help_position=40, width=80)

    def _format_action_invocation(self, action):
        if not action.option_strings or action.nargs == 0:
            return super()._format_action_invocation(action)
        default = self._get_default_metavar_for_optional(action)
        args_string = self._format_args(action, default)
        return ", ".join(action.option_strings) + " " + args_string


class SlurmFormatter:
    """ """

    token_re = r"%\.?[0-9]*[^%\s]?"
    token_pad_re = r"\.*[0-9]+"

    @staticmethod
    def parse_time(time):
        """
        turn a bunch of seconds into something human readable
        """
        time = int(time)

        # Convert time into hours, minutes, and seconds.
        hours = time // 3600 % 24
        minutes = time // 60 % 60
        seconds = time % 60

        if hours > 0:
            return f"{hours}:{minutes:02}:{seconds:02}"

        return f"{minutes:02}:{seconds:02}"

    def get_header_dict(self):
        """
        print header for output
        """
        headers = {
            "%": "",
            "%a": "USERNAME",
            "%i": "JOBID",
            "%P": "PARTITION",
            "%j": "NAME",
            "%u": "USER",
            "%t": "STATUS",
            "%M": "TIME",
            "%D": "NODES",
            "%R": "NODELIST(REASON)",
        }

        return headers

    def get_job_dict(self, job):
        """
        Print a row based on the information from a job and format_string.
        """
        reasonnode = job.sched.reason_pending
        if str(job.sched.reason_pending) == "":
            reasonnode = job.nodelist

        values = {
            "%": "",
            "%a": job.username,
            "%i": job.id.f58,
            "%P": str(job.sched.queue),
            "%j": job.name,
            "%u": job.username,
            "%t": job.status_abbrev,
            "%M": self.parse_time(job.runtime),
            "%D": job.nnodes,
            "%R": reasonnode,
        }

        return values

    def format(self, format_string, types_dict):
        """
        format output to minimc slurm's format language
        """
        result = []
        prev_end = 0

        # Search for tokens that begin with % in the format string.
        for token in re.finditer(self.token_re, format_string):

            # Copy text in string before format token to the result text.
            prefix = format_string[prev_end : token.start()]
            result.append(prefix)

            # To match the behavior of squeue's format option we should
            # ignore unknown tokens and print them as regular text after
            # issuing a warning.
            try:
                # Users may specify padding modifiers before tokens in
                # the form of %.10u or %10u to indicate a prefix or suffix
                # padding respectively. Find and extract padding modifiers
                # if present.
                width_match = re.match(self.token_pad_re, token.group()[1:])
                if width_match is not None:
                    width_string = width_match.group()
                    output = types_dict[token.group().replace(width_string, "")]
                    if width_string[0] == ".":
                        width = int(width_string[1:])
                        output = f"{output:>{width}}"[:width]
                    else:
                        width = int(width_string)
                        output = f"{output:<{width}}"[:width]
                else:
                    output = types_dict[token.group()]

                result.append(output)

            except KeyError:
                continue

            prev_end = token.end()

        # Add the remainder of format string after last token to result.
        result.append(format_string[prev_end:])
        return "".join(result)

    def get_unknown_tokens(self, format_string):
        """
        Return a list of unknown tokens based on the types_dict given.
        """
        headers = self.get_header_dict()
        unknown_tokens = []
        for token in re.finditer(self.token_re, format_string):
            width_match = re.match(self.token_pad_re, token.group()[1:])
            key = token.group()
            try:
                if width_match is not None:
                    width_string = width_match.group()
                    key = key.replace(width_string, "")
                    headers[key]
                else:
                    headers[key]
            except KeyError:
                unknown_tokens.append(key)
        return unknown_tokens


def argwarn(argval):
    """
    print a warning for unsupported arguments
    """
    return (
        f'WARNING: "{argval}" is not supported by this wrapper and is being ignored.\n'
        'WARNING: fsqueue is a wrapper script for the native "flux jobs" command.\n'
        'See "flux help jobs" or contact the LC Hotline for help using the native commands.'
    )


def main(parsedargs):
    args, unknown_args = parsedargs
    if unknown_args:
        print(argwarn(" ".join(unknown_args)), file=sys.stderr)

    # Set user if explicitly specified.
    user = "all"
    if args.user is not None:
        user = args.user

    # Set default job_states for search.
    job_states = ["pending", "running"]

    # Validate explicit job state if given.
    if args.state is not None:
        known_states = {
            "running": "running",
            "r": "running",
            "pending": "pending",
            "pd": "pending",
            "all": "active",
        }

        # Normalize and search for state in known states.
        if args.state.lower() in known_states.keys():
            job_states = [known_states[args.state.lower()]]
        else:
            print(
                f"Invalid job state specified: {args.state}",
                file=sys.stderr,
            )
            print(
                f"Valid job states include: {','.join(known_states.keys())}",
                file=sys.stderr,
            )
            exit(1)

    # Start with an empty list of job ids and append ids if explicity
    # defined by the user as an argument.
    job_ids = []
    if args.jobs is not None:
        for id in args.jobs.split(","):
            job_ids.append(flux.job.JobID(id))

    # Initialize a connection to flux.
    conn = flux.Flux()

    # Retrieve a list of jobs and attributes from flux. If job_ids is not empty
    # the search will be limited to the jobs specified. Otherwise flux will
    # return a full list of all jobs matching the other filters we've specified.
    rpc = flux.job.JobList(
        conn, user=user, ids=job_ids, filters=job_states
    ).fetch_jobs()
    jobs = list(rpc.get_jobinfos())

    # Further filter jobs so that all jobs are running on a node in
    # args.nodelist if a nodelist was specified by the user.
    if args.nodelist is not None:
        nodelist = flux.hostlist.Hostlist(args.nodelist)
        jobs = [job for job in jobs if job.nodelist in nodelist]

    formatter = SlurmFormatter()

    unknown_tokens = formatter.get_unknown_tokens(args.format)
    for token in unknown_tokens:
        print(f"Invalid job format specification: {token[1]}", file=sys.stderr)

    if args.noheader is False:
        headers_dict = formatter.get_header_dict()
        print(formatter.format(args.format, headers_dict))

    for job in jobs:
        job_dict = formatter.get_job_dict(job)
        print(formatter.format(args.format, job_dict))


if __name__ == "__main__":

    def fmt(prog):
        return CustomHelpFormatter(prog)

    parser = argparse.ArgumentParser(
        description="List running and queued jobs in squeue format.",
        conflict_handler="resolve",
        allow_abbrev=False,
        formatter_class=fmt,
    )

    parser.add_argument("-u", "--user", metavar="<user>", help="show jobs run by user")
    parser.add_argument(
        "-t",
        "--state",
        metavar="<state>",
        help='use "-t all" to show jobs in all states, including completed jobs',
    )
    parser.add_argument(
        "-j",
        "--jobs",
        metavar="<jobid>,<jobid>,....",
        help="display only jobs specified",
    )
    parser.add_argument(
        "-h", "--noheader", action="store_true", help="do not print a header"
    )
    parser.add_argument(
        "-o",
        "--format",
        metavar="<format>",
        default="%.18i %.9P %.8j %.8u %.2t %.10M %.6D %R",
        help="format specification",
    )

    parser.add_argument(
        "-w", "--nodelist", metavar="<nodelist>", help="show jobs that ran on nodelist"
    )

    main(parser.parse_known_args())

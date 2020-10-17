import requests
import re
import os
import matplotlib.pyplot as plot
from datetime import datetime, timedelta
from subprocess import run

from ignored_files import ignored_files
from github_pat import token

url_clone = "https://github.com"
url_api = "https://api.github.com"
owner = "weee-open"
output_file = "stats"
is_organization = True
generate_graphs = True


def raise_rate_limited_exception():
    raise Exception("You are getting rate-limited by GitHub's servers. Try again in a few minutes.") from None


def raise_cloc_not_installed_exception():
    raise Exception("cloc is not installed.\n"
                    "Install it from https://github.com/AlDanial/cloc or use wc to count lines") from None


def get_repos(header: dict) -> list:
    url = f'{url_api}/{"orgs" if is_organization else "users"}/{owner}/repos?per_page=100'
    pages = 1

    # As of the time of writing, we don't *need* pagination as we have < 100 repos, but just for future proofing
    # here is code that can handle n pages of repositories
    try:
        response = requests.get(url, headers=header)
        repos = [repo['name'] for repo in response.json() if not repo['archived'] and not repo['disabled']]

        # If the result page is only one page long, no link header is present
        if 'link' in response.headers:
            for link in response.headers['link'].split(','):
                location, rel = link.split(';')

                if rel.strip() == 'rel="last"':
                    pages = int(re.compile('&page=(?P<page>[0-9]+)').search(location).group('page'))

            for page in range(2, (pages + 1)):
                response = requests.get(f'{url}&page={page}', headers=header)
                repos += [repo['name'] for repo in response.json() if not repo['archived'] and not repo['disabled']]

        # ignore case when sorting list of repos to prevent uppercase letters to come before lowercase letters
        return sorted(repos, key=str.casefold)

    except TypeError:
        raise_rate_limited_exception()


def get_anonymous_commits_stats(repos: list, header: dict) -> dict:
    # see https://docs.github.com/en/free-pro-team@latest/rest/reference/repos#statistics
    stats = {'total': 0}

    print("\nGetting anonymous commits stats...")
    for i, repo in enumerate(repos):
        response = requests.get(f"{url_api}/repos/{owner}/{repo}/stats/commit_activity", headers=header)
        print(f"{i + 1}/{len(repos)} - {repo} - {'OK' if response.status_code == 200 else 'Awaiting new data...'}")

        if response.status_code == 403:
            raise_rate_limited_exception()
        elif response.status_code == 200:
            stats[repo] = sum([weekly['total'] for weekly in response.json()])
            stats['total'] += stats[repo]

    print("\n")
    return stats


def get_contributors_commits_stats(repos: list, header: dict) -> dict:
    # see https://docs.github.com/en/free-pro-team@latest/rest/reference/repos#get-all-contributor-commit-activity
    stats = {'total': {}, 'past_year': {}}
    unix_one_year_ago = int((datetime.now() - timedelta(days=365)).timestamp())

    print("Getting contributors commits stats...")
    for i, repo in enumerate(repos):
        response = requests.get(f"{url_api}/repos/{owner}/{repo}/stats/contributors", headers=header)
        print(f"{i + 1}/{len(repos)} - {repo} - {'OK' if response.status_code == 200 else 'Awaiting new data...'}")

        if response.status_code == 403:
            raise_rate_limited_exception()

        elif response.status_code == 200:
            json = response.json()
            stats[repo] = {
                'total': {author['author']['login']: author['total']
                          for author in json},
                'past_year': {author['author']['login']: sum(week['c']
                                                             for week in author['weeks']
                                                             if week['w'] > unix_one_year_ago)
                              for author in json},
            }

            for author in json:
                login = author['author']['login']
                if login not in stats['total']:
                    stats['total'][login] = 0
                    stats['past_year'][login] = 0
                stats['total'][login] += author['total']
                stats['past_year'][login] += sum(week['c']
                                                 for week in author['weeks']
                                                 if week['w'] > unix_one_year_ago)

    print("\n")
    return stats


def _cleanup_repos(repos: list):
    for repo in repos:
        run(f"rm -rf {repo}".split())


def get_lines_stats(repos: list, use_cloc: bool) -> dict:
    stats = {'total': {'sloc': 0, 'all': 0}} if use_cloc else {'total': 0}
    ignored = " ".join([f"':!:{file}'" for file in ignored_files])
    _cleanup_repos(repos)

    print("Getting SLOC stats...")
    for i, repo in enumerate(repos):
        run(f"git clone {url_clone}/{owner}/{repo}".split())

        if use_cloc:
            try:
                cloc_out = run(f"cloc --csv {repo}",
                               shell=True,
                               text=True,
                               capture_output=True).stdout.splitlines()[-1]

                stats[repo] = {
                    'sloc': int(cloc_out.split(",")[-1]) or 0,
                    'comments': int(cloc_out.split(",")[-2]) or 0,
                    'blanks': int(cloc_out.split(",")[-3]) or 0,
                }

                stats['total']['sloc'] += stats[repo]['sloc']
                stats['total']['all'] += stats[repo]['sloc'] + stats[repo]['comments'] + stats[repo]['blanks']

            except IndexError:
                raise_cloc_not_installed_exception()

        else:
            git_files = run(f"cd {repo} && git ls-files -- . {ignored} && cd ..",
                            shell=True, text=True, capture_output=True).stdout.splitlines()
            # remove blank / whitespace-only lines
            for file in git_files:
                run(f"sed '/^\s*$/d' {repo}/{file} &> /dev/null", shell=True)

            stats[repo] = int(run(f"cd {repo} && wc -l $(git ls-files -- . {ignored}) && cd ..",
                                  shell=True,
                                  text=True,
                                  capture_output=True).stdout.splitlines()[-1].split(" ")[-2])

            stats['total'] += stats[repo]

        print(f"{i + 1}/{len(repos)} -- {stats[repo]['sloc'] if use_cloc else stats[repo]} "
              f"total non-blank lines in repo {repo}")
        run(f"rm -rf {repo}".split())

    return stats


def generate_chart(data: dict, minimum: int, type: str, legend: str, title: str, path: str):
    # Remove summatory keys from the dictionary.
    # The additional 'nope' is there just to avoid having to put everything in a try in case the "total" key does not exist. 
    data.pop('total', 'nope')
    data.pop('past_year', 'nope')

    other = 0

    for k in list(data):
        if data[k] < minimum:
            other += data.pop(k)

    if other != 0:
        data['other'] = other

    keys = data.keys()
    values = data.values()
    count = len(values)

    if type == 'pie':
        colors = []
        figure, axis = plot.subplots(subplot_kw=dict(aspect='equal'))

        # Set the color map and generate a properly sized color cycle
        colormaps = {'Pastel1':9, 'Accent':8, 'Set1':9, 'tab20':20, 'tab20b':20}

        for cm in colormaps:
            cmap = plot.get_cmap(cm)
            colors += [cmap(i/colormaps[cm]) for i in range(colormaps[cm])]

        step = int(len(colors)/count)
        axis.set_prop_cycle('color', [colors[i*step] for i in range(count)])

        wedges, texts = axis.pie(values)
        legend = axis.legend(wedges, keys, title=legend, bbox_to_anchor=(1.01, 1), loc='upper left')
        axis.set_title(title)

        plot.savefig(path, bbox_extra_artists=(legend,), bbox_inches='tight')
    
    elif type == 'bar':
        if count < 2:
            return

        # Dimensions of an A4 paper in inches are 8.27x11.69
        # Make the graph dimentions proportional to the number of columns. In this way, we have consistent bar heights.
        figure, axis = plot.subplots(figsize=(12, 0.4 + 0.2*count), dpi=600)

        y = [i for i in range(count)]

        axis.barh(y, values, align='center')
        axis.set_yticks(y)
        axis.set_yticklabels(keys)
        axis.invert_yaxis()
        axis.set_xlabel(legend)
        axis.set_title(title)

        for i, v in enumerate(values):
            axis.text(v + int(v/10), i, str(v), va='center', color='black', fontweight='bold')

        plot.savefig(path, bbox_inches='tight')
    
    plot.close(figure)


def print_all_stats(commits_stats: dict, lines_stats: dict, contributors_stats: dict, use_cloc: bool):
    if(generate_graphs):
        graph_dir = datetime.now().strftime("%Y-%m-%d_%H:%M:%S.%f")
        os.mkdir(graph_dir)

    if commits_stats is not None:
        if(generate_graphs):
            generate_chart(dict(commits_stats), 10, 'pie', 'Repositories', 'Total commits to all repositories in the last year', f'{graph_dir}/yearly_commits_by_repo.svg')

        commits_output = "\n".join([f"{repo}: {commits_stats[repo]} commits past year"
                                    for repo in commits_stats
                                    if repo != "total"])

        commits_output += f"\nTotal commits of past year: {commits_stats['total']}"
    else:
        commits_output = "No commits stats, as you've selected at the beginning."

    if contributors_stats is not None:
        if(generate_graphs):
            generate_chart(dict(contributors_stats['total']), 1, 'bar', 'Commits', 'Total commits from all members', f'{graph_dir}/total_commits.svg')
            generate_chart(dict(contributors_stats['past_year']), 1, 'bar', 'Commits', 'Total commits from all members last year', f'{graph_dir}/last_year_commits.svg')

        for repo in contributors_stats:
            if repo not in ['total', 'past_year']:
                os.mkdir(f'{graph_dir}/{repo}')
                generate_chart(dict(contributors_stats[repo]['total']), 1, 'bar', 'Commits', 'Total commits from all contributors', f'{graph_dir}/{repo}/total_commits.svg')
                generate_chart(dict(contributors_stats[repo]['past_year']), 1, 'bar', 'Commits', 'Total commits from all contributors last year', f'{graph_dir}/{repo}/past_year_commits.svg')

        # I know using replace like this is really bad, I just don't want to spend years parsing the output
        contributors_output = "\n".join([f"{repo}: {contributors_stats[repo]}"
                                         .replace("'", "")
                                         .replace(": {", "\n\t")
                                         .replace("}, ", "\n\t")
                                         .replace("}} ", "\n")
                                         .replace("}", "")
                                         .replace("total\n\t", "total:\n\t\t")
                                         .replace("past_year\n\t", "past year:\n\t\t")
                                         for repo in contributors_stats
                                         if repo not in ["total", "past_year"]])

        # sort by number of commits
        contributors_stats['total'] = {k: v for k, v in sorted(contributors_stats['total'].items(),
                                                               key=lambda item: item[1],
                                                               reverse=True)}

        contributors_stats['past_year'] = {k: v for k, v in sorted(contributors_stats['past_year'].items(),
                                                                   key=lambda item: item[1],
                                                                   reverse=True)}

        contributors_output += f"\nTotal all time:\n\t{contributors_stats['total']}" \
                               .replace("'", "") \
                               .replace(", ", "\n\t") \
                               .replace("{", "") \
                               .replace("}", "")
        contributors_output += f"\nPast year:\n\t{contributors_stats['past_year']}" \
                               .replace("'", "") \
                               .replace(", ", "\n\t") \
                               .replace("{", "") \
                               .replace("}", "")

    else:
        contributors_output = ""

    lines_output = ""

    if lines_stats is not None:
        if use_cloc:
            lines_output = "\n".join([f"{repo}: {lines_stats[repo]['sloc']} sloc - "
                                      f"{lines_stats[repo]['comments']} comments - "
                                      f"{lines_stats[repo]['blanks']} blank lines - "
                                      f"{lines_stats[repo]['sloc'] + lines_stats[repo]['comments'] + lines_stats[repo]['blanks']} total"
                                      for repo in lines_stats
                                      if repo != "total"])
            lines_output += f"\nTotal SLOC: {lines_stats['total']['sloc']}" \
                            f"\nTotal lines including comments and blanks: {lines_stats['total']['all']}"

        else:
            lines_output = "\n".join([f"{repo}: {lines_stats[repo]} lines total"
                                      for repo in lines_stats
                                      if repo != "total"])
            lines_output += f"\nTotal SLOC: {lines_stats['total']}"

    output = "\n\n".join([contributors_output, '*' * 42, commits_output, '*' * 42, lines_output])
    print(f"\n\n{output}")

    output_path = f'{output_file} {datetime.now()}.txt' if not generate_graphs else f'{graph_dir}/stats.txt'
    with open(output_path, 'w') as out:
        out.write(f"Stats generated via https://github.com/weee-open/sardina\n"
                  f"use_cloc={use_cloc}\n"
                  f"\n{output}")


def main():
    use_cloc = input("Do you want to use cloc (C) or wc (W) to count SLOC? c/W ").lower() == "c"
    get_commits = input("Do you want to get the commits stats? It may take a long time due to GitHub servers updating "
                        "their cache. y/N ").lower() == "y"
    get_lines = input("Do you want to get the SLOC stats? It may take a long time since it has to clone each repository. y/N").lower() == "y"
    generate_graphs = input("Do you want to generate graphs for the statistics? y/N ").lower() == 'y'

    header = {'Authorization': f"token {token}"} if token != "YOUR TOKEN HERE" else {}

    repos = get_repos(header)
    commits_stats = get_anonymous_commits_stats(repos, header) if get_commits else None
    contributors_stats = get_contributors_commits_stats(repos, header) if get_commits else None
    lines_stats = get_lines_stats(repos, use_cloc) if get_lines else None
    print_all_stats(commits_stats, lines_stats, contributors_stats, use_cloc)


if __name__ == "__main__":
    main()

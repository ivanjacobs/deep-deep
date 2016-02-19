# -*- coding: utf-8 -*-
"""
Adaptive crawling algorithm.

It works by training a link classifier at the same time crawl is happening.
This classifer is then used to direct crawler to more promising links.

The first assumption is that all links to the same page are similar
if they are from the same domain. Because crawler works in-domain
it means we don't have to turn off dupefilter, and that there is no
need to handle all incoming links to a page - it is enough to
consider only one. This means instead of a general crawl graph
we're working with a crawl tree.
"""

import itertools
import os
import time
import random
import collections
import datetime

import networkx as nx
from twisted.internet.task import LoopingCall
from sklearn.externals import joblib
import scrapy

from acrawler.spiders.base import BaseSpider
from acrawler.utils import (
    get_response_domain,
    set_request_domain,
    ensure_folder_exists,
)
from acrawler import score_links
from acrawler.score_pages import (
    available_form_types,
    get_constant_scores,
    response_max_scores,
)


class AdaptiveSpider(BaseSpider):
    """
    Adaptive spider. It crawls a a list of URLs using adaptive algorithm
    and stores the results to ./checkpoints folder.

    Example::

        scrapy crawl adaptive -a seeds_url=./urls.csv -L INFO

    With domain name as a feature::

        scrapy crawl adaptive -a seeds_url=./urls.csv -a fit_domain_inercept=1 -L INFO

    """
    name = 'adaptive'
    custom_settings = {
        'DEPTH_LIMIT': 5,
        # 'DEPTH_PRIORITY': 1,
        # 'CONCURRENT_REQUESTS':
    }

    # Crawler arguments
    fit_domain_intercept = 0  # whether to learn per-domain intercept
    converge = 0  # whether SGD should converge
    replay_N = 0  # how many links to take for experience replay
    epsilon = 0  # probability of choosing a random link instead of
                 # the the most promising
    positive_weight = 20  # how much more impact positive cases make
                          # FIXME: hardcoded constant for all form types

    # intervals for periodic tasks
    stats_interval = 10
    checkpoint_interval = 60*10
    update_link_scores_interval = 30

    # autogenerated crawl name
    crawl_id = str(datetime.datetime.now())

    # crawl graph
    G = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fit_domain_intercept = bool(int(self.fit_domain_intercept))
        self.converge = bool(int(self.converge))
        self.replay_N = int(self.replay_N)
        self.epsilon = float(self.epsilon)
        self.positive_weight = float(self.positive_weight)

        self.logger.info("CRAWL {}: fit_domain_intercept={}, converge={}, replay_N={}, eps={}".format(
            self.crawl_id, self.fit_domain_intercept, self.converge, self.replay_N, self.epsilon
        ))

        self.G = nx.DiGraph(name='Crawl Graph')
        self.node_ids = itertools.count()
        self.seen_urls = set()
        self._replay_node_ids = set()
        self._scores_recalculated_at = 0
        self.domain_scores = MaxScores(available_form_types())

        self.log_task = LoopingCall(self.print_stats)
        self.log_task.start(self.stats_interval, now=False)
        self.checkpoint_task = LoopingCall(self.checkpoint)
        self.checkpoint_task.start(self.checkpoint_interval, now=False)
        self.update_link_scores_task = LoopingCall(self.recalculate_link_scores)
        self.update_link_scores_task.start(self.update_link_scores_interval, now=False)

        self.link_vectorizer = score_links.get_vectorizer(
            use_hashing=True,
            use_domain=self.fit_domain_intercept,
        )
        self.link_classifiers = {
            form_cls: score_links.get_classifier(
                positive_weight=self.positive_weight,
                converge=self.converge,
            )
            for form_cls in available_form_types()
        }
        ensure_folder_exists(self._data_path(''))
        self.logger.info("Crawl {} started".format(self.crawl_id))

    def parse(self, response):
        self.increase_response_count()

        node_id = self.update_response_node(response)

        self.update_domain_scores(response, node_id)
        self.update_classifiers(node_id)

        if not self.G.node[node_id]['ok']:
            return  # don't send requests from failed responses

        yield from self.generate_out_nodes(response, node_id)

        # TODO:
        # self.update_classifiers_bootstrapped(node_id)

    def update_response_node(self, response):
        """
        Update crawl graph with information about the received response.
        Return node_id of the node which corresponds to this response.
        """
        node_id = response.meta.get('node_id')

        # 1. Handle seed responses which don't yet have node_id
        is_seed_url = node_id is None
        if is_seed_url:
            node_id = next(self.node_ids)

        # 2. Update node with observed information
        ok = response.status == 200 and hasattr(response, 'text')
        if ok:
            observed_scores = response_max_scores(response)
        else:
            observed_scores = get_constant_scores(0.0)

        self.G.add_node(
            node_id,
            url=response.url,
            visited=True,
            ok=ok,
            scores=observed_scores,
            response_id=self.response_count,
        )

        if not is_seed_url:
            # don't add initial nodes to replay because there is
            # no incoming links for such nodes
            self._replay_node_ids.add(node_id)

        return node_id

    def on_offdomain_request_dropped(self, request):
        super().on_offdomain_request_dropped(request)

        node_id = request.meta.get('node_id')
        if not node_id:
            self.logger.warn("Request without node_id dropped: {}".format(request))
            return

        self.G.add_node(
            node_id,
            visited=True,
            ok=False,
            scores=get_constant_scores(0.0),
            response_id=self.response_count,
        )
        self._replay_node_ids.add(node_id)

    def update_domain_scores(self, response, node_id):
        domain = get_response_domain(response)
        scores = self.G.node[node_id]['scores']
        if not scores:
            return
        self.domain_scores.update(domain, scores)
        self.scheduler_queue.update_observed_scores(response, scores)

    @property
    def scheduler_queue(self):
        return self.crawler.engine.slot.scheduler.queue

    def recalculate_link_scores(self):
        """ Update scores of all links in a frontier """
        self._recalculate_link_scores(
            min_resp_count=max(500, len(self.domain_scores)),
        )

    def _recalculate_link_scores(self, min_resp_count):
        if min_resp_count is not None:
            interval = self.response_count - self._scores_recalculated_at
            if interval <= min_resp_count:
                self.logger.info(
                    "Fewer than {} classifier updates ({}); not re-classifying links.".format(
                    min_resp_count, interval
                ))
                return

        self.logger.info("Re-classifying links: prepare...")
        links = []
        update_node_ids = []
        for node_id in self.scheduler_queue.iter_active_node_ids():
            for prev_id in self.G.predecessors_iter(node_id):
                links.append(self.G.edge[prev_id][node_id]['link'])
                update_node_ids.append(node_id)

        self.logger.info("Re-classifying links: classifying {} links...".format(
            len(links))
        )
        link_scores = self.get_link_scores(links, verbose=True)

        self.logger.info("Re-classifying links: updating crawl graph...")
        for node_id, scores in zip(update_node_ids, link_scores):
            self.G.add_node(node_id, predicted_scores=scores)

        self.logger.info("Re-classifying links: updating queues...")
        self.scheduler_queue.recalculate_priorities()
        self.logger.info("Re-classifying links: done")
        self._scores_recalculated_at = self.response_count

    def generate_out_nodes(self, response, this_node_id):
        """
        Extract links from the response and add nodes and edges to crawl graph.
        Returns an iterator of scrapy.Request objects.
        """

        # Extract in-domain links and their features
        domain = get_response_domain(response)

        # Generate nodes, edges and requests based on link information
        links = list(self.iter_link_dicts(response, domain))
        random.shuffle(links)

        link_scores = self.get_link_scores(links)

        for link, scores in zip(links, link_scores):
            url = link['url']

            # generate nodes and edges
            node_id = next(self.node_ids)
            self.G.add_node(
                node_id,
                url=url,
                visited=False,
                ok=None,
                scores=None,
                response_id=None,
                predicted_scores=scores,
            )
            self.G.add_edge(this_node_id, node_id, link=link)

            # generate Scrapy request
            request = scrapy.Request(url, meta={
                'handle_httpstatus_list': [403, 404, 500],
                'node_id': node_id,
            }, priority=0)
            set_request_domain(request, domain)
            yield request

    def update_classifiers(self, node_id):
        """ Update classifiers based on information received at node_id """
        node = self.G.node[node_id]
        assert node['visited']

        # We got scores for this node_id; it means we can use incoming links
        # as training data.
        # Note: because of the way we crawl there is always either 0 or 1
        # incoming links.
        X_raw = list(self._iter_incoming_link_dicts(node_id))
        if not X_raw:
            return

        y_all = {}
        for form_type in self.link_classifiers:
            y_all[form_type] = self._get_y(node_id, form_type) * len(X_raw)

        # Experience replay: select N random training examples from the past.
        if self.replay_N and self.replay_N < len(self._replay_node_ids):
            past_node_ids = random.sample(self._replay_node_ids, self.replay_N)
            for _id in past_node_ids:
                _x = list(self._iter_incoming_link_dicts(_id))
                if not _x:
                    continue
                X_raw.extend(_x)
                for form_type in self.link_classifiers:
                    y_all[form_type].extend(self._get_y(_id, form_type) * len(_x))

        # Vectorize input and update the model
        X = self.link_vectorizer.transform(X_raw)

        for form_type, clf in self.link_classifiers.items():
            y = y_all[form_type]
            clf.partial_fit(X, y, classes=[False, True])

    def _iter_incoming_link_dicts(self, node_id):
        for prev_id in self.G.predecessors_iter(node_id):
            yield self.G.edge[prev_id][node_id]['link']

    def _get_y(self, node_id, form_type):
        node = self.G.node[node_id]
        return [node['scores'].get(form_type, 0.0) >= 0.5]

    def update_classifiers_bootstrapped(self, node_id):
        """ Update classifiers based on outgoing link scores """
        # TODO
        raise NotImplementedError()

    def get_link_scores(self, links, verbose=False):
        """ Classify links and return a list of their score dicts """
        if not links:
            return []
        if verbose:
            self.logger.info("get_link_scores: vectorizing...")
        X = self.link_vectorizer.transform(links)
        if verbose:
            self.logger.info("get_link_scores: classifying...")
        scores = [{} for _ in links]
        for form_type, clf in self.link_classifiers.items():
            if clf.coef_ is None:
                # Not fitted yet; assign uniform probabilities.
                # TODO: investigate optimistic initialization to help
                # with initial exploration?
                probs = [0.5] * len(links)
            else:
                probs = clf.predict_proba(X)[..., 1]

            for prob, score_dict in zip(probs, scores):
                score_dict[form_type] = prob
        return scores

    def print_stats(self):
        active_downloads = len(self.crawler.engine.downloader.active)
        self.logger.info("Active downloads: {}".format(active_downloads))
        msg = "Crawl graph: {} nodes ({} visited), {} edges, {} domains".format(
            self.G.number_of_nodes(),
            self.response_count,
            self.G.number_of_edges(),
            len(self.domain_scores)
        )
        self.logger.info(msg)

        scores_sum = sorted(self.domain_scores.sum().items())
        scores_avg = sorted(self.domain_scores.avg().items())
        reward_lines = [
            "{:8.1f}   {:0.4f}   {}".format(tot, avg, k)
            for ((k, tot), (k, avg)) in zip(scores_sum, scores_avg)
        ]
        msg = '\n'.join(reward_lines)
        self.logger.info("Reward (total / average): \n{}".format(msg))
        self.logger.info("Total reward: {}".format(sum(s for k, s in scores_sum)))

    def checkpoint(self):
        """
        Save current crawl state, which can be analyzed while
        the crawl is still going.
        """
        ts = int(time.time())
        graph_filename = 'crawl-{}.pickle.gz'.format(ts)
        clf_filename = 'classifiers-{}.joblib'.format(ts)
        self.save_crawl_graph(graph_filename)
        self.save_classifiers(clf_filename)

    def save_crawl_graph(self, path):
        self.logger.info("Saving crawl graph...")
        nx.write_gpickle(self.G, self._data_path(path))
        self.logger.info("Crawl graph saved")

    def save_classifiers(self, path):
        self.logger.info("Saving classifiers...")
        pipe = {
            'vec': self.link_vectorizer,
            'clf': self.link_classifiers,
        }
        joblib.dump(pipe, self._data_path(path), compress=3)
        self.logger.info("Classifiers saved")

    def _data_path(self, path):
        return os.path.join('checkpoints', self.crawl_id, path)

    def closed(self, reason):
        """ Save crawl graph to a file when spider is closed """
        tasks = [
            self.log_task,
            self.checkpoint_task,
            self.update_link_scores_task
        ]
        for task in tasks:
            if task.running:
                task.stop()
        self.save_classifiers('classifiers.joblib')
        self.save_crawl_graph('crawl.pickle.gz')


class MaxScores:
    """
    >>> s = MaxScores(['x', 'y'])
    >>> s.update("foo", {"x": 0.1, "y": 0.3})
    >>> s.update("foo", {"x": 0.01, "y": 0.4})
    >>> s.update("bar", {"x": 0.8})
    >>> s.sum() == {'x': 0.9, 'y': 0.4}
    True
    >>> s.avg() == {'x': 0.45, 'y': 0.2}
    True
    >>> len(s)
    2
    """
    def __init__(self, classes):
        self.classes = classes
        self._zero_scores = {form_type: 0.0 for form_type in self.classes}
        self.scores = collections.defaultdict(lambda: self._zero_scores.copy())

    def update(self, domain, scores):
        cur_scores = self.scores[domain]
        for k, v in scores.items():
            cur_scores[k] = max(cur_scores[k], v)

    def sum(self):
        return {
            k: sum(v[k] for v in self.scores.values())
            for k in self.classes
        }

    def avg(self):
        if not self.scores:
            return self._zero_scores.copy()
        return {k: v/len(self.scores) for k, v in self.sum().items()}

    def __len__(self):
        return len(self.scores)

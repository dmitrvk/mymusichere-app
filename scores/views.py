from django.http import HttpResponse
from django.shortcuts import render, get_object_or_404
from django.views import generic, View
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.utils.decorators import method_decorator

from git import Repo
import subprocess

import os
import re
import hmac
import hashlib
import logging

from django.conf import settings
from .models import Score


class IndexView(generic.ListView):
    template_name = 'scores/index.html'
    queryset = Score.objects.all()

    def get(self, request):
        self.request.session.set_test_cookie()
        return super().get(request)


class ScoreView(generic.DetailView):
    model = Score
    template_name = 'scores/score.html'

    logger = logging.getLogger(__name__)

    def get_object(self):
        score = super().get_object()

        if self.request.session.test_cookie_worked():
            self.request.session.delete_test_cookie()
            if not self.request.session.get('viewed_score', False):
                score.views += 1
                score.save()
                self.request.session['viewed_score'] = True

        self.logger.info("Score '%s' accessed" % score.slug)

        return score


class PublishView(View):
    SPACE = r'\s*'
    LINE_BEGIN = r'^' + SPACE
    EQUALS_SIGN = SPACE + r'=' + SPACE
    VALUE = r'".*"'

    HEADER_START_PATTERN = r'\\header'
    TITLE_PATTERN = LINE_BEGIN + r'title' + EQUALS_SIGN + VALUE
    COMPOSER_PATTERN = LINE_BEGIN + r'composer' + EQUALS_SIGN + VALUE
    ARRANGER_PATTERN = LINE_BEGIN + r'arranger' + EQUALS_SIGN + VALUE
    INSTRUMENT_PATTERN = LINE_BEGIN + r'instruments*' + EQUALS_SIGN + VALUE

    logger = logging.getLogger(__name__)
    repo_dir = os.path.join(settings.BASE_DIR, 'scores', 'lilypond', 'out', 'scores')

    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super(PublishView, self).dispatch(request, *args, **kwargs)

    def post(self, request):
        if self.__is_request_valid(request):
            try:
                self.__delete_scores_removed_from_repo()
                self.__update_changed_scores()
                self.__create_scores_added_to_repo()
                return HttpResponse('DB updated successfully')
            except Exception as e:
                return HttpResponse('Failed to update DB. %s' % e, status=500)
        else:
            return HttpResponse('Wrong request', status=400)


    def __is_request_valid(self, request):
        if 'Authorization' in request.headers:
            header = request.headers.get('Authorization', 'None').split()
            return header[0] == 'Token' and header[1] == settings.PUBLISH_TOKEN
        else:
            return False

    def __get_repo_scores(self):
        return set([f.name for f in os.scandir(self.repo_dir) if f.is_dir()])

    def __get_db_scores(self):
        return set([score.slug for score in Score.objects.all()])

    def __delete_scores_removed_from_repo(self):
        repo_scores = self.__get_repo_scores()
        db_scores = self.__get_db_scores()

        scores_to_delete = db_scores.difference(repo_scores)

        Score.objects.filter(slug__in=scores_to_delete).delete()

        if scores_to_delete:
            self.logger.info("Scores '%s' deleted" % "','".join(scores_to_delete))
        else:
            self.logger.info('No scores deleted')

    def __update_changed_scores(self):
        scores_to_update = self.__get_db_scores()
        updated_scores = []

        for slug in scores_to_update:
            db_score = Score.objects.filter(slug=slug)[0]
            repo_score = self.__create_score_from_header(slug)
            if db_score != repo_score:
                db_score.update_with_score(repo_score)
                db_score.save()
            updated_scores.append(slug)

        if updated_scores:
            self.logger.info("Scores '%s' updated" % "','".join(updated_scores))
        else:
            self.logger.info('No scores updated')

    def __create_scores_added_to_repo(self):
        repo_scores = self.__get_repo_scores()
        db_scores = self.__get_db_scores()

        new_scores = repo_scores.difference(db_scores)

        for slug in new_scores:
            self.__create_score_from_header(slug).save()

        if new_scores:
            self.logger.info("Scores '%s' created" % "','".join(new_scores))
        else:
            self.logger.info('No scores created')

    def __create_score_from_header(self, score_slug):
        score = Score(title='', slug=score_slug)

        path_to_source = os.path.join(
            settings.MYMUSICHERE_REPO_DIR,
            score.slug,
            '%s.ly' % score.slug
        )

        reading_header = False
        for line in open(path_to_source):
            if not reading_header and re.search(self.HEADER_START_PATTERN, line):
                reading_header = True
            else:
                if '}' in line:
                    reading_header = False
                else:
                    match = re.search(self.TITLE_PATTERN, line)
                    if match and not score.title:
                        score.title = match.group().split('"')[1]
                        continue

                    match = re.search(self.COMPOSER_PATTERN, line)
                    if match and not score.composer:
                        score.composer = match.group().split('"')[1]
                        continue

                    match = re.search(self.ARRANGER_PATTERN, line)
                    if match and not score.arranger:
                        score.arranger = match.group().split('"')[1]
                        continue

                    match = re.search(self.INSTRUMENT_PATTERN, line)
                    if match and not score.instrument:
                        score.instrument = match.group().split('"')[1]
                        continue

        return score


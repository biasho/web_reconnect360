import datetime
import json
import threading
from datetime import timedelta
from django.contrib import messages
from django.db import close_old_connections, transaction
from django.db.models import F, Prefetch, Q,Avg, Count,OuterRef,Subquery
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import Lead, Source
from questions.models import Option, Question
import logging,random

logger = logging.getLogger(__name__)
from .models import (
    Answer,
    CompositeSurvey,
    Submission,
    Survey,
    SurveyResultThreshold,
    SurveySubmission,
)
from .services.send_mail_pdf import generate_pdf_and_send_email

# ==========================================
# HELPER FUNCTIONS
# ==========================================

def _get_survey_by_slug(slug):
    """Lấy Survey bằng ID tách từ đuôi Slug hoặc trực tiếp bằng Slug."""
    try:
        survey_id = slug.split('-')[-1]
        return get_object_or_404(Survey, pk=survey_id, is_active=True)
    except (ValueError, IndexError):
        return get_object_or_404(Survey, slug=slug, is_active=True)


# ==========================================
# PUBLIC & SUBMISSION VIEWS
# ==========================================

def survey_list(request):
    """Danh sách các khảo sát đang hoạt động."""
    surveys = Survey.objects.filter(is_active=True)
    return render(request, 'pages/index.html')
def survey_document(request):
    context = {
        'danh_sach_lop': ['Lớp 6', 'Lớp 7', 'Lớp 8', 'Lớp 9', 'Lớp 10', 'Lớp 11', 'Lớp 12'],
        'gioi_tinh': ['Nam', 'Nữ', 'Khác', 'Không muốn trả lời'],
        'mxh_thoigian': ['Dưới 1h', 'Từ 1-2h', 'Từ 2-4h', 'Từ 4-6h', 'Trên 6h'],
    }
    return render(request, 'surveys/reconnect360/survey_document.html', context)
def survey_detail(request, slug):
    """Chi tiết một khảo sát đơn."""
    survey = get_object_or_404(Survey, slug=slug, is_active=True)
    
    questions = (
        survey.questions
        .select_related('group')
        .prefetch_related('options')
        .all()
        .order_by('group__code', 'id')
    )
    
    return render(request, 'surveys/single/detail.html', {
        'survey': survey,
        'questions': questions
    })


def submit_detail(request, slug):
    """Xử lý nộp bài khảo sát đơn: Tương thích hoàn toàn với models.py."""
    try:
        survey_id = slug.split('-')[-1]
        survey = get_object_or_404(Survey, pk=survey_id, is_active=True)
    except (ValueError, IndexError):
        survey = get_object_or_404(Survey, slug=slug, is_active=True)

    if request.method == 'POST':
        try:
            user = request.user if request.user.is_authenticated else None
            lead = None
            source_obj = Source.objects.filter(name='survey_single').first() or Source.objects.filter(name='survey_reconnect360').first()

            if user:
                lead, _ = Lead.objects.get_or_create(
                    user=user,
                    defaults={
                        'email': user.email,
                        'full_name': (f"{user.first_name} {user.last_name}".strip() or user.username),
                        'source': source_obj,
                    },
                )
            else:
                if not request.session.session_key:
                    request.session.create()

                lead_id_from_session = request.session.get('survey_lead_id')
                if lead_id_from_session:
                    lead = Lead.objects.filter(id=lead_id_from_session).first()

                if not lead:
                    lead = Lead.objects.create(full_name="Học sinh vãng lai", source=source_obj)
                    request.session['survey_lead_id'] = lead.id

            total_score = 0
            answers_to_create = []
            group_scores = {}

            with transaction.atomic():
                submission = Submission.objects.create(
                    composite_survey=None,
                    survey=survey,
                    user=user,
                    lead=lead,
                    email=lead.email if lead else None,
                    session_key=request.session.session_key,
                    total_score=0,
                )

                processed_mc_questions = set()

                for key in request.POST.keys():
                    if key.startswith('question_') and '_text_' not in key:
                        try:
                            question_id = int(key.split('_')[1])
                        except (IndexError, ValueError):
                            continue

                        question = Question.objects.select_related('group').filter(id=question_id, surveys=survey).first()
                        if not question:
                            continue

                        if question.question_type in ['single_choice', 'rating']:
                            val = request.POST.get(key)
                            selected_option = Option.objects.filter(id=val, question=question).first()
                            if selected_option:
                                option_score = getattr(selected_option, 'score', getattr(selected_option, 'point', 0))
                                total_score += option_score

                                if question.group and question.group.code:
                                    g_code = question.group.code.upper().strip()
                                    group_scores[g_code] = group_scores.get(g_code, 0) + option_score

                                answers_to_create.append(
                                    Answer(submission=submission, question=question, selected_option=selected_option)
                                )

                        elif question.question_type == 'multiple_choice':
                            if question_id in processed_mc_questions:
                                continue
                            processed_mc_questions.add(question_id)

                            selected_ids = request.POST.getlist(key)
                            for opt_id in selected_ids:
                                selected_option = Option.objects.filter(id=opt_id, question=question).first()
                                if selected_option:
                                    custom_text = request.POST.get(f'question_{question.id}_text_{opt_id}', '').strip()
                                    option_score = getattr(selected_option, 'score', getattr(selected_option, 'point', 0))
                                    total_score += option_score

                                    if question.group and question.group.code:
                                        g_code = question.group.code.upper().strip()
                                        group_scores[g_code] = group_scores.get(g_code, 0) + option_score

                                    answers_to_create.append(
                                        Answer(
                                            submission=submission,
                                            question=question,
                                            selected_option=selected_option,
                                            text_answer=custom_text if custom_text else "",
                                        )
                                    )

                        elif question.question_type == 'text':
                            text_val = request.POST.get(key, '').strip()
                            if text_val:
                                answers_to_create.append(
                                    Answer(submission=submission, question=question, text_answer=text_val)
                                )

                Answer.objects.bulk_create(answers_to_create)

                SurveySubmission.objects.update_or_create(
                    submission=submission,
                    survey=survey,
                    defaults={'score': total_score}
                )

                submission.total_score = total_score
                submission.score_a = group_scores.get('A', 0)
                submission.score_b = group_scores.get('B', 0)
                submission.score_c = group_scores.get('C', 0)
                submission.score_d = group_scores.get('D', 0)
                submission.score_difficulties = submission.score_a + submission.score_b + submission.score_c
                submission.save()

                request.session['last_submission_id'] = submission.id

            messages.success(request, "Cảm ơn bạn đã hoàn thành bài khảo sát!")
            return redirect('surveys:survey_thankyou', slug=survey.slug)

        except Exception as e:
            logger.error(f"Error submitting survey {slug}: {str(e)}", exc_info=True)
            messages.error(request, f"Có lỗi xảy ra trong quá trình nộp bài: {str(e)}")
            return redirect('surveys:survey_detail', slug=survey.slug)

    return redirect('surveys:survey_detail', slug=survey.slug)


def survey_thankyou(request, slug):
    """Hiển thị trang cảm ơn sau khi hoàn thành khảo sát."""
    survey = get_object_or_404(Survey, slug=slug, is_active=True)
    submission_id = request.session.get('last_submission_id')
    submission = Submission.objects.filter(id=submission_id).first() if submission_id else None

    return render(request, 'surveys/single/thankyou.html', {
        'survey': survey,
        'submission': submission
    })


def reconnect360_detail(
    request,
    slug="khao-sat-xu-huong-thu-minh-va-muc-do-ket-noi-xa-hoi-o-hoc-sinh-thcs-thpt-1",
):
    """Chi tiết khảo sát tổng hợp Reconnect360."""
    composite = get_object_or_404(CompositeSurvey, slug=slug, is_active=True)

    questions_prefetch = Prefetch(
        'survey__questions',
        queryset=Question.objects.select_related('group')
        .order_by('group__code', 'id')
        .prefetch_related('options'),
    )

    survey_items = (
        composite.items.select_related('survey')
        .prefetch_related(questions_prefetch)
        .order_by('order')
    )

    return render(request, 'surveys/reconnect360/detail.html', {'composite': composite, 'survey_items': survey_items})


def submit_survey(request, slug):
    """Xử lý nộp bài khảo sát Reconnect360 và lưu kết quả."""
    composite = get_object_or_404(CompositeSurvey, slug=slug, is_active=True)

    if request.method == 'POST':
        user = None
        lead = None
        source_obj = Source.objects.filter(name='survey_reconnect360').first()

        if request.user.is_authenticated:
            user = request.user
            lead, _ = Lead.objects.get_or_create(
                user=user,
                defaults={
                    'email': user.email,
                    'full_name': (f"{user.first_name} {user.last_name}".strip() or user.username),
                    'source': source_obj,
                },
            )
        else:
            if not request.session.session_key:
                request.session.create()

            lead_id_from_session = request.session.get('survey_lead_id')
            if lead_id_from_session:
                lead = Lead.objects.filter(id=lead_id_from_session).first()

            if not lead:
                lead = Lead.objects.create(full_name="Học sinh vãng lai", source=source_obj)
                request.session['survey_lead_id'] = lead.id

        total_score = 0
        answers_to_create = []
        survey_scores = {}
        group_scores = {}
        group_names = {}

        question_to_survey_map = {}
        items = composite.items.select_related('survey').prefetch_related('survey__questions')
        for item in items:
            for q in item.survey.questions.all():
                question_to_survey_map[q.id] = item.survey.id

        with transaction.atomic():
            submission = Submission.objects.create(
                composite_survey=composite,
                user=user,
                lead=lead,
                email=lead.email if lead else None,
                session_key=request.session.session_key,
                total_score=0,
            )

            processed_mc_questions = set()

            for key in request.POST.keys():
                if key.startswith('question_') and '_text_' not in key:
                    try:
                        question_id = int(key.split('_')[1])
                    except (IndexError, ValueError):
                        continue

                    question = Question.objects.select_related('group').filter(id=question_id).first()
                    if not question:
                        continue

                    parent_survey_id = question_to_survey_map.get(question.id)

                    if question.question_type in ['single_choice', 'rating']:
                        val = request.POST.get(key)
                        selected_option = Option.objects.filter(id=val, question=question).first()
                        if selected_option:
                            option_score = getattr(selected_option, 'score', getattr(selected_option, 'point', 0))
                            total_score += option_score

                            if parent_survey_id:
                                survey_scores[parent_survey_id] = survey_scores.get(parent_survey_id, 0) + option_score

                            if question.group:
                                g_code = question.group.code.upper().strip()
                                group_scores[g_code] = group_scores.get(g_code, 0) + option_score
                                if g_code not in group_names:
                                    group_names[g_code] = question.group.name or f"Nhóm {g_code}"

                            answers_to_create.append(
                                Answer(submission=submission, question=question, selected_option=selected_option)
                            )

                    elif question.question_type == 'multiple_choice':
                        if question_id in processed_mc_questions:
                            continue
                        processed_mc_questions.add(question_id)

                        selected_ids = request.POST.getlist(key)
                        for opt_id in selected_ids:
                            selected_option = Option.objects.filter(id=opt_id, question=question).first()
                            if selected_option:
                                custom_text = request.POST.get(f'question_{question.id}_text_{opt_id}', '').strip()
                                option_score = getattr(selected_option, 'score', getattr(selected_option, 'point', 0))
                                total_score += option_score

                                if parent_survey_id:
                                    survey_scores[parent_survey_id] = survey_scores.get(parent_survey_id, 0) + option_score

                                if question.group:
                                    g_code = question.group.code.upper().strip()
                                    group_scores[g_code] = group_scores.get(g_code, 0) + option_score
                                    if g_code not in group_names:
                                        group_names[g_code] = question.group.name or f"Nhóm {g_code}"

                                answers_to_create.append(
                                    Answer(
                                        submission=submission,
                                        question=question,
                                        selected_option=selected_option,
                                        text_answer=custom_text if custom_text else "",
                                    )
                                )

                    elif question.question_type == 'text':
                        text_val = request.POST.get(key, '').strip()
                        if text_val:
                            answers_to_create.append(
                                Answer(submission=submission, question=question, text_answer=text_val)
                            )

            Answer.objects.bulk_create(answers_to_create)

            survey_submissions_to_create = [
                SurveySubmission(submission=submission, survey_id=s_id, score=s_score)
                for s_id, s_score in survey_scores.items()
            ]
            SurveySubmission.objects.bulk_create(survey_submissions_to_create)

            submission.total_score = total_score
            submission.score_a = group_scores.get('A', 0)
            submission.score_b = group_scores.get('B', 0)
            submission.score_c = group_scores.get('C', 0)
            submission.score_d = group_scores.get('D', 0)
            submission.score_difficulties = submission.score_a + submission.score_b + submission.score_c
            submission.save()
            request.session['last_submission_id'] = submission.id

        # CHUYỂN HƯỚNG SANG VIEW KẾT QUẢ RIÊNG BIỆT (NGĂN CHẶN LỖI F5)
        return redirect('surveys:survey_result', slug=composite.slug)

    return redirect('surveys:reconnect360_detail', slug=composite.slug)


def survey_result_view(request, slug):
    """View chuyên hiển thị kết quả Reconnect360 bằng phương thức GET (an toàn khi F5)."""
    composite = get_object_or_404(CompositeSurvey, slug=slug, is_active=True)
    
    submission_id = request.session.get('last_submission_id')
    if submission_id:
        submission = Submission.objects.filter(id=submission_id, composite_survey=composite).first()
    else:
        if request.user.is_authenticated:
            submission = Submission.objects.filter(composite_survey=composite, user=request.user).order_by('-created_at').first()
        else:
            submission = Submission.objects.filter(composite_survey=composite, session_key=request.session.session_key).order_by('-created_at').first()

    if not submission:
        return redirect('surveys:reconnect360_detail', slug=composite.slug)

    score_abc = submission.score_a + submission.score_b + submission.score_c
    score_d = submission.score_d
    total_score = submission.total_score

    threshold_abc = None
    threshold_d = None
    all_thresholds_abc = []
    all_thresholds_d = []

    for item in composite.items.select_related('survey').all():
        s_obj = item.survey

        if not threshold_abc:
            t_abc = SurveyResultThreshold.objects.filter(
                survey=s_obj,
                min_score__lte=score_abc,
                max_score__gte=score_abc,
                group__code__iexact='all_abc',
            ).first()
            if t_abc:
                threshold_abc = t_abc
                all_thresholds_abc = list(
                    SurveyResultThreshold.objects.filter(
                        survey=s_obj, group__code__iexact='all_abc'
                    ).order_by('min_score')
                )

        if not threshold_d:
            t_d = SurveyResultThreshold.objects.filter(
                survey=s_obj,
                min_score__lte=score_d,
                max_score__gte=score_d,
                group__code__iexact='D',
            ).first()
            if t_d:
                threshold_d = t_d
                all_thresholds_d = list(
                    SurveyResultThreshold.objects.filter(
                        survey=s_obj, group__code__iexact='D'
                    ).order_by('min_score')
                )

    chart_abc_json = (
        json.dumps({
            'labels': [f"{t.title} ({t.min_score}-{t.max_score}đ)" for t in all_thresholds_abc],
            'max_scores': [t.max_score for t in all_thresholds_abc],
            'user_score': score_abc,
        })
        if all_thresholds_abc
        else json.dumps({'labels': [], 'max_scores': [], 'user_score': score_abc})
    )

    chart_d_json = (
        json.dumps({
            'labels': [f"{t.title} ({t.min_score}-{t.max_score}đ)" for t in all_thresholds_d],
            'max_scores': [t.max_score for t in all_thresholds_d],
            'user_score': score_d,
        })
        if all_thresholds_d
        else json.dumps({'labels': [], 'max_scores': [], 'user_score': score_d})
    )

    context = {
        'composite': composite,
        'submission': submission,
        'total_score': total_score,
        'score_abc': score_abc,
        'score_d': score_d,
        'threshold_abc': threshold_abc,
        'threshold_d': threshold_d,
        'chart_abc_json': chart_abc_json,
        'chart_d_json': chart_d_json,
    }
    return render(request, 'surveys/survey_result.html', context)


@require_POST
def send_email_result(request):
    """View xử lý AJAX gửi Email chứa PDF báo cáo từ Popup Modal."""
    email = request.POST.get('email', '').strip()
    submission_id = request.POST.get('submission_id') or request.session.get('last_submission_id')

    if not email:
        return JsonResponse({'success': False, 'message': 'Vui lòng cung cấp địa chỉ email!'}, status=400)

    if not submission_id:
        return JsonResponse(
            {'success': False, 'message': 'Không tìm thấy thông tin lượt nộp bài. Vui lòng thử lại!'},
            status=400,
        )

    try:
        submission = Submission.objects.select_related('composite_survey', 'lead').get(pk=submission_id)

        if submission.lead and not submission.lead.email:
            submission.lead.email = email
            submission.lead.save(update_fields=['email'])

        score_abc = getattr(
            submission,
            'score_difficulties',
            (
                getattr(submission, 'score_a', 0)
                + getattr(submission, 'score_b', 0)
                + getattr(submission, 'score_c', 0)
            ),
        )
        score_d = getattr(submission, 'score_d', 0)

        threshold_abc = None
        threshold_d = None

        if submission.composite_survey:
            for item in submission.composite_survey.items.select_related('survey').all():
                s_obj = item.survey

                if not threshold_abc:
                    t_abc = SurveyResultThreshold.objects.filter(
                        survey=s_obj,
                        min_score__lte=score_abc,
                        max_score__gte=score_abc,
                        group__code__iexact='all_abc',
                    ).first()
                    if t_abc:
                        threshold_abc = t_abc

                if not threshold_d:
                    t_d = SurveyResultThreshold.objects.filter(
                        survey=s_obj,
                        min_score__lte=score_d,
                        max_score__gte=score_d,
                        group__code__iexact='D',
                    ).first()
                    if t_d:
                        threshold_d = t_d

                if threshold_abc and threshold_d:
                    break
        elif getattr(submission, 'survey', None):
            s_obj = submission.survey
            threshold_abc = SurveyResultThreshold.objects.filter(
                survey=s_obj,
                min_score__lte=score_abc,
                max_score__gte=score_abc,
                group__code__iexact='all_abc',
            ).first()

            threshold_d = SurveyResultThreshold.objects.filter(
                survey=s_obj,
                min_score__lte=score_d,
                max_score__gte=score_d,
                group__code__iexact='D',
            ).first()

        survey_data = {
            'total_score': getattr(submission, 'total_score', 0),
            'abc_score': score_abc,
            'abc_level': threshold_abc.title if threshold_abc else 'Mức độ chưa xác định',
            'abc_comment': getattr(threshold_abc, 'description', '') if threshold_abc else 'Chưa có nhận xét chi tiết.',
            'abc_recommendation': getattr(threshold_abc, 'recommendation', '') if threshold_abc else '',
            'd_score': score_d,
            'd_level': threshold_d.title if threshold_d else 'Mức độ chưa xác định',
            'd_comment': getattr(threshold_d, 'description', '') if threshold_d else 'Chưa có nhận xét chi tiết.',
            'd_recommendation': getattr(threshold_d, 'recommendation', '') if threshold_d else '',
            'max_score_abc': 72,
            'max_score_d': 24,
        }

        def _async_send_email():
            try:
                generate_pdf_and_send_email(email, survey_data)
            finally:
                close_old_connections()

        threading.Thread(target=_async_send_email).start()

        return JsonResponse({
            'success': True,
            'message': 'Gửi báo cáo PDF thành công! Vui lòng kiểm tra hòm thư của bạn.',
        })

    except Submission.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Không tìm thấy lượt nộp bài này!'}, status=404)
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Lỗi hệ thống: {str(e)}'}, status=500)


# ==========================================
# REPORTING VIEWS
# ==========================================

def reconnect_report_view(
    request,
    slug="khao-sat-xu-huong-thu-minh-va-muc-do-ket-noi-xa-hoi-o-hoc-sinh-thcs-thpt-1",
):
    survey = _get_survey_by_slug(slug)
    time_filter = request.GET.get('time_filter', 'current_year')
    year_from = request.GET.get('year_from')
    year_to = request.GET.get('year_to')

    if hasattr(Submission, 'survey'):
        submissions = Submission.objects.filter(
            Q(survey=survey) | Q(composite_survey__items__survey=survey)
        ).distinct()
    else:
        submissions = Submission.objects.filter(composite_survey__items__survey=survey).distinct()

    now = timezone.now()
    date_field = 'submitted_at'

    if time_filter == 'current_year':
        submissions = submissions.filter(**{f'{date_field}__year': now.year})
    elif time_filter == '3_months':
        submissions = submissions.filter(**{f'{date_field}__gte': now - timedelta(days=90)})
    elif time_filter == '6_months':
        submissions = submissions.filter(**{f'{date_field}__gte': now - timedelta(days=180)})
    elif time_filter == '1_year':
        submissions = submissions.filter(**{f'{date_field}__gte': now - timedelta(days=365)})
    elif time_filter == 'last_year':
        submissions = submissions.filter(**{f'{date_field}__year': now.year - 1})
    elif time_filter == 'custom' and year_from and year_to:
        try:
            submissions = submissions.filter(**{
                f'{date_field}__year__gte': int(year_from),
                f'{date_field}__year__lte': int(year_to),
            })
        except ValueError:
            pass

    total_participants = submissions.count()
    completion_rate = 100.0 if total_participants > 0 else 0

    thresholds = SurveyResultThreshold.objects.filter(survey=survey).order_by('min_score')

    threshold_stats = [
        {
            'title': t.title,
            'min_score': t.min_score,
            'max_score': t.max_score,
            'count': 0,
        }
        for t in thresholds
    ]

    for sub in submissions:
        score = sub.total_score
        for stat in threshold_stats:
            if stat['min_score'] <= score <= stat['max_score']:
                stat['count'] += 1
                break

    data = {
        'total_participants': total_participants,
        'total_completed': total_participants,
        'completion_rate': completion_rate,
        'threshold_stats': threshold_stats,
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse(data)

    return render(request, 'surveys/reconnect_report.html', {'survey': survey, **data})


def reconnect360_report_view(
    request,
    slug="khao-sat-xu-huong-thu-minh-va-muc-do-ket-noi-xa-hoi-o-hoc-sinh-thcs-thpt-1",
):
    survey = _get_survey_by_slug(slug) if slug else Survey.objects.filter(is_active=True).first()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        data = _get_reconnect360_report_data(survey, request)
        return JsonResponse(data)

    return render(request, "surveys/reconnect360/report.html", {"survey": survey})


def _get_reconnect360_report_data(survey, request):
    time_filter = request.GET.get("time_filter", "current_year")
    now = timezone.now()

    if not survey:
        return {
            "status": "error",
            "message": "Không tìm thấy khảo sát!",
            "total_participants": 0,
            "total_surveys": 0,
            "total_completed": 0,
            "completion_rate": 0,
            "group_abc": {"chart_data": {"labels": [], "series": []}, "table_data": []},
            "group_d": {"chart_data": {"labels": [], "series": []}, "table_data": []},
        }

    survey_submissions = SurveySubmission.objects.filter(survey=survey).select_related('submission')
    date_field = "submission__submitted_at"

    if time_filter == "today":
        survey_submissions = survey_submissions.filter(**{f"{date_field}__date": now.date()})
    elif time_filter == "this_week":
        start_of_week = (now - datetime.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        survey_submissions = survey_submissions.filter(**{f"{date_field}__gte": start_of_week})
    elif time_filter == "1_month":
        survey_submissions = survey_submissions.filter(**{f"{date_field}__gte": now - datetime.timedelta(days=30)})
    elif time_filter == "3_months":
        survey_submissions = survey_submissions.filter(**{f"{date_field}__gte": now - datetime.timedelta(days=90)})
    elif time_filter == "6_months":
        survey_submissions = survey_submissions.filter(**{f"{date_field}__gte": now - datetime.timedelta(days=180)})
    elif time_filter == "1_year":
        survey_submissions = survey_submissions.filter(**{f"{date_field}__gte": now - datetime.timedelta(days=365)})
    elif time_filter == "last_year":
        survey_submissions = survey_submissions.filter(**{f"{date_field}__year": now.year - 1})
    elif time_filter == "current_year":
        survey_submissions = survey_submissions.filter(**{f"{date_field}__year": now.year})
    elif time_filter == "custom":
        year_from = request.GET.get("year_from")
        year_to = request.GET.get("year_to")
        if year_from and year_to and year_from.isdigit() and year_to.isdigit():
            survey_submissions = survey_submissions.filter(**{
                f"{date_field}__year__gte": int(year_from),
                f"{date_field}__year__lte": int(year_to),
            })

    total_surveys = survey_submissions.count()

    users_count = survey_submissions.filter(
        submission__user__isnull=False
    ).values("submission__user").distinct().count()

    has_lead = hasattr(Submission, 'lead')
    if has_lead:
        leads_count = survey_submissions.filter(
            submission__user__isnull=True,
            submission__lead__isnull=False
        ).values("submission__lead").distinct().count()
    else:
        leads_count = 0

    guests_filter = {"submission__user__isnull": True, "submission__session_key__isnull": False}
    if has_lead:
        guests_filter["submission__lead__isnull"] = True

    sessions_count = survey_submissions.filter(
        **guests_filter
    ).values("submission__session_key").distinct().count()

    total_participants = users_count + leads_count + sessions_count

    thresholds_abc = SurveyResultThreshold.objects.filter(
        survey=survey, group__code__iexact='all_abc'
    ).order_by("min_score")

    labels_abc, series_abc, table_abc = [], [], []

    for t in thresholds_abc:
        count = survey_submissions.annotate(
            score_abc=F('submission__score_a') + F('submission__score_b') + F('submission__score_c')
        ).filter(
            score_abc__gte=t.min_score, score_abc__lte=t.max_score
        ).count()

        pct = f"{round((count / total_surveys) * 100, 1)}%" if total_surveys > 0 else "0.0%"
        labels_abc.append(t.title)
        series_abc.append(count)
        table_abc.append({
            "title": t.title,
            "score_range": f"{t.min_score} – {t.max_score} điểm",
            "count": count,
            "percentage": pct,
            "description": getattr(t, 'description', ''),
            "recommendation": getattr(t, 'recommendation', ''),
        })

    thresholds_d = SurveyResultThreshold.objects.filter(
        survey=survey, group__code__iexact='D'
    ).order_by("min_score")

    labels_d, series_d, table_d = [], [], []

    for t in thresholds_d:
        count = survey_submissions.filter(
            submission__score_d__gte=t.min_score,
            submission__score_d__lte=t.max_score
        ).count()

        pct = f"{round((count / total_surveys) * 100, 1)}%" if total_surveys > 0 else "0.0%"
        labels_d.append(t.title)
        series_d.append(count)
        table_d.append({
            "title": t.title,
            "score_range": f"{t.min_score} – {t.max_score} điểm",
            "count": count,
            "percentage": pct,
            "description": getattr(t, 'description', ''),
            "recommendation": getattr(t, 'recommendation', ''),
        })

    return {
        "status": "success",
        "total_participants": total_participants,
        "total_surveys": total_surveys,
        "total_completed": total_surveys,
        "completion_rate": 100 if total_surveys > 0 else 0,
        "group_abc": {
            "chart_data": {"labels": labels_abc, "series": series_abc},
            "table_data": table_abc,
        },
        "group_d": {
            "chart_data": {"labels": labels_d, "series": series_d},
            "table_data": table_d,
        },
    }


def combo_detail(request, slug):
    """Chi tiết bộ khảo sát (CompositeSurvey hoặc Survey kèm thông tin liên quan)."""
    composite = CompositeSurvey.objects.filter(slug=slug, is_active=True).first()
    
    if composite:
        survey_items = composite.items.select_related('survey').prefetch_related(
            Prefetch(
                'survey__questions',
                queryset=Question.objects.select_related('group').order_by('group__code', 'id').prefetch_related('options')
            )
        ).order_by('order')

        context = {
            'composite': composite,
            'survey_items': survey_items,
        }
        return render(request, 'surveys/combo/detail.html', context)
    
    survey = get_object_or_404(Survey, slug=slug, is_active=True)
    questions = survey.questions.select_related('group').prefetch_related('options').all().order_by('group__code', 'id')
    
    context = {
        'survey': survey,
        'questions': questions,
    }
    return render(request, 'surveys/combo/detail.html', context)

#

def survey_statistics_view(request, survey_id):
    # Lấy thông tin khảo sát
    survey = get_object_or_404(Survey, id=survey_id)
    questions = (
        survey.questions
        .select_related('group')
        .prefetch_related('options')
        .all()
        .order_by('group__code', 'id')
    )
    
    # Lấy các tham số lọc & sắp xếp từ Query String
    filter_type = request.GET.get('filter', '')
    start_date_str = request.GET.get('start_date', '')
    end_date_str = request.GET.get('end_date', '')
    sort_order = request.GET.get('sort', 'desc')
    
    # Khởi tạo queryset cơ bản cho Answer
    answers_qs = Answer.objects.filter(question__in=questions)
    
    # Xử lý lọc theo loại bộ lọc
    if filter_type == 'today':
        now = timezone.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        answers_qs = answers_qs.filter(submission__submitted_at__range=(start_of_day, end_of_day))
    else:
        # Xử lý start_date nếu có
        if start_date_str:
            try:
                dt_start = datetime.datetime.strptime(start_date_str, '%Y-%m-%dT%H:%M')
                if timezone.is_naive(dt_start):
                    dt_start = timezone.make_aware(dt_start)
                answers_qs = answers_qs.filter(submission__submitted_at__gte=dt_start)
            except ValueError:
                pass
                
        # Xử lý end_date nếu có
        if end_date_str:
            try:
                dt_end = datetime.datetime.strptime(end_date_str, '%Y-%m-%dT%H:%M')
                if timezone.is_naive(dt_end):
                    dt_end = timezone.make_aware(dt_end)
                answers_qs = answers_qs.filter(submission__submitted_at__lte=dt_end)
            except ValueError:
                pass

    stats_data = []
    for q in questions:
        # Lọc danh sách câu trả lời thuộc câu hỏi hiện tại từ queryset đã áp dụng filter thời gian
        q_answers = answers_qs.filter(question=q)
        
        choice_counts = {}
        text_answers = [] # Lưu các câu trả lời dạng chữ (tự luận)
        
        for ans in q_answers:
            if hasattr(ans, 'text_answer') and ans.text_answer:
                text_answers.append(ans.text_answer)
            elif hasattr(ans, 'selected_option') and ans.selected_option:
                choice_text = ans.selected_option
                choice_counts[choice_text] = choice_counts.get(choice_text, 0) + 1
            else:
                val = getattr(ans, 'answer_text', None) or str(ans)
                text_answers.append(val)
        
        # Sắp xếp các lựa chọn trắc nghiệm theo số lượng chọn
        sorted_choices = sorted(
            choice_counts.items(), 
            key=lambda item: item[1], 
            reverse=(sort_order == 'desc')
        )
        
        stats_data.append({
            'question': q,
            'sorted_choices': sorted_choices,
            'text_answers': text_answers,
            'total_answers': q_answers.count()
        })

    context = {
        'survey': survey,
        'stats_data': stats_data,
        'current_sort': sort_order,
        'filter_type': filter_type,
        'start_date': start_date_str,
        'end_date': end_date_str,
    }
    return render(request, 'surveys/single/survey_statistics.html', context)
#
def cross_analysis_report_view(request, slug="khao-sat-xu-huong-thu-minh-va-muc-do-ket-noi-xa-hoi-o-hoc-sinh-thcs-thpt-1"):
    """View hiển thị báo cáo Phân tích Tương quan Chéo chuyên sâu có tích hợp bộ lọc thời gian."""
    survey = _get_survey_by_slug(slug) if slug else Survey.objects.filter(is_active=True).first()
    
    survey_submissions = SurveySubmission.objects.filter(survey=survey).select_related('submission')
    date_field = "submission__submitted_at"

    # Lấy các tham số lọc từ request GET
    filter_type = request.GET.get('filter')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    # Xử lý logic lọc thời gian linh hoạt
    current_tz = timezone.get_current_timezone()
    time_label = ""

    # Hàm hỗ trợ format datetime sang định dạng HH:MM ngày DD/MM/YYYY
    def format_dt_display(dt):
        return dt.strftime("%H:%M ngày %d/%m/%Y")

    if filter_type == 'today':
        # Lọc trong ngày hôm nay
        today = timezone.localdate()
        start_of_day = timezone.make_aware(datetime.datetime.combine(today, datetime.time.min), current_tz)
        end_of_day = timezone.make_aware(datetime.datetime.combine(today, datetime.time.max), current_tz)
        survey_submissions = survey_submissions.filter(**{f"{date_field}__gte": start_of_day, f"{date_field}__lte": end_of_day})
        time_label = "trong ngày hôm nay"
    
    elif filter_type == 'before' and end_date_str:
        # Lọc trước mốc thời gian (sử dụng giá trị từ ô end_date truyền lên)
        try:
            naive_dt = datetime.datetime.fromisoformat(end_date_str)
            aware_dt = timezone.make_aware(naive_dt, current_tz)
            survey_submissions = survey_submissions.filter(**{f"{date_field}__lte": aware_dt})
            time_label = f"Tính đến {format_dt_display(naive_dt)}"
        except ValueError:
            pass

    elif filter_type == 'after' and start_date_str:
        # Lọc sau mốc thời gian (sử dụng giá trị từ ô start_date truyền lên)
        try:
            naive_dt = datetime.datetime.fromisoformat(start_date_str)
            aware_dt = timezone.make_aware(naive_dt, current_tz)
            survey_submissions = survey_submissions.filter(**{f"{date_field}__gte": aware_dt})
            time_label = f"Từ {format_dt_display(naive_dt)} trở đi"
        except ValueError:
            pass

    else:
        # Mặc định hoặc lọc theo khoảng thời gian thông thường (Từ ... Đến ...)
        start_dt_obj = None
        end_dt_obj = None

        if start_date_str:
            try:
                naive_start = datetime.datetime.fromisoformat(start_date_str)
                aware_start = timezone.make_aware(naive_start, current_tz)
                survey_submissions = survey_submissions.filter(**{f"{date_field}__gte": aware_start})
                start_dt_obj = naive_start
            except ValueError:
                pass

        if end_date_str:
            try:
                naive_end = datetime.datetime.fromisoformat(end_date_str)
                aware_end = timezone.make_aware(naive_end, current_tz)
                survey_submissions = survey_submissions.filter(**{f"{date_field}__lte": aware_end})
                end_dt_obj = naive_end
            except ValueError:
                pass

        # Tạo chuỗi nhãn thời gian hiển thị theo kết quả khoảng thời gian lọc
        if start_dt_obj and end_dt_obj:
            time_label = f"Từ {format_dt_display(start_dt_obj)} đến {format_dt_display(end_dt_obj)}"
        elif start_dt_obj:
            time_label = f"Từ {format_dt_display(start_dt_obj)} trở đi"
        elif end_dt_obj:
            time_label = f"Tính đến {format_dt_display(end_dt_obj)}"

        # Nếu hoàn toàn không có tham số lọc nào, áp dụng mốc mặc định
        if not filter_type and not start_date_str and not end_date_str:
            naive_deadline = datetime.datetime(2026, 8, 27, 22, 0, 0)
            target_deadline = timezone.make_aware(naive_deadline, current_tz)
            survey_submissions = survey_submissions.filter(**{f"{date_field}__lte": target_deadline})
            time_label = f"Tính đến {format_dt_display(naive_deadline)}"

    # Nếu vẫn chưa có nhãn thời gian (do lỗi parse hoặc lấy toàn bộ)
    if not time_label:
        time_label = "Toàn bộ thời gian"

    # Tổng số lượt khảo sát thỏa điều kiện lọc
    total_surveys = survey_submissions.count()

    # Tính toán điểm và phân loại nhóm Thu mình (A+B+C)
    annotated_subs = survey_submissions.annotate(
        score_abc=F('submission__score_a') + F('submission__score_b') + F('submission__score_c')
    )

    # Lọc nhóm Thu mình Cao (Mức 3 & 4 từ 37 điểm trở lên)
    high_withdrawal_subs = annotated_subs.filter(score_abc__gte=37)
    high_withdrawal_count = high_withdrawal_subs.count()

    # Trong nhóm Thu mình Cao, phân tách theo Nhu cầu Kết nối Cao (Mức 3 & 4 của D, score_d >= 13)
    high_connection_in_high_withdrawal = high_withdrawal_subs.filter(submission__score_d__gte=13).count()
    low_connection_in_high_withdrawal = high_withdrawal_count - high_connection_in_high_withdrawal

    # Tính tỷ lệ phần trăm chính xác dựa trên dữ liệu thực tế đã lọc
    cross_high_pct = round((high_connection_in_high_withdrawal / high_withdrawal_count) * 100, 1) if high_withdrawal_count > 0 else 0.0
    cross_low_pct = round((low_connection_in_high_withdrawal / high_withdrawal_count) * 100, 1) if high_withdrawal_count > 0 else 0.0

    context = {
        'survey': survey,
        'total_surveys': total_surveys,
        'high_withdrawal_count': high_withdrawal_count,
        'cross_high_pct': cross_high_pct,
        'cross_low_pct': cross_low_pct,
        'filter_type': filter_type or '',
        'start_date': start_date_str or '',
        'end_date': end_date_str or '',
        'time_label': time_label,  # Truyền trực tiếp chuỗi nhãn thời gian đã được format chuẩn
    }

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({
            "status": "success",
            **context
        })

    return render(request, "surveys/reconnect360/cross_analysis.html", context)
#
def reconnect360_demographics_view(
    request,
    slug="khao-sat-xu-huong-thu-minh-va-muc-do-ket-noi-xa-hoi-o-hoc-sinh-thcs-thpt-1",
):
    # Lấy chính xác bài khảo sát theo slug
    survey = Survey.objects.filter(slug=slug, is_active=True).first()
    if not survey:
        survey = Survey.objects.filter(is_active=True).first()

    # Kiểm tra nếu là AJAX request thì trả về JSON data cho biểu đồ & bảng
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        data = _get_reconnect360_report_demographics_data(survey, request)
        return JsonResponse(data)

    return render(
        request, 
        "surveys/reconnect360/demographics.html", 
        {"survey": survey}
    )


def _get_time_filtered_queryset(queryset, request, date_field="submitted_at"):
    """Hàm phụ trợ giúp lọc queryset theo khoảng thời gian chuẩn xác."""
    time_filter = request.GET.get("time_filter", "current_year")
    now = timezone.now()
    prefix = f"{date_field}__" if date_field else ""

    if time_filter == "today":
        return queryset.filter(**{f"{prefix}date": now.date()})
    elif time_filter == "this_week":
        start_of_week = (now - datetime.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return queryset.filter(**{f"{prefix}gte": start_of_week})
    elif time_filter == "1_month":
        return queryset.filter(**{f"{prefix}gte": now - datetime.timedelta(days=30)})
    elif time_filter == "3_months":
        return queryset.filter(**{f"{prefix}gte": now - datetime.timedelta(days=90)})
    elif time_filter == "6_months":
        return queryset.filter(**{f"{prefix}gte": now - datetime.timedelta(days=180)})
    elif time_filter == "1_year":
        return queryset.filter(**{f"{prefix}gte": now - datetime.timedelta(days=365)})
    elif time_filter == "last_year":
        return queryset.filter(**{f"{prefix}year": now.year - 1})
    elif time_filter == "current_year":
        return queryset.filter(**{f"{prefix}year": now.year})
    elif time_filter == "custom_year":
        year_from = request.GET.get("year_from")
        year_to = request.GET.get("year_to")
        filters = {}
        if year_from and year_from.isdigit():
            filters[f"{prefix}year__gte"] = int(year_from)
        if year_to and year_to.isdigit():
            filters[f"{prefix}year__lte"] = int(year_to)
        if filters:
            return queryset.filter(**filters)
    elif time_filter == "custom_date":
        date_from = request.GET.get("date_from")
        date_to = request.GET.get("date_to")
        filters = {}
        if date_from:
            filters[f"{prefix}date__gte"] = date_from
        if date_to:
            filters[f"{prefix}date__lte"] = date_to
        if filters:
            return queryset.filter(**filters)
            
    return queryset

def _get_reconnect360_report_demographics_data(survey, request):
    if not survey:
        return {
            "status": "error",
            "message": "Không tìm thấy khảo sát!",
            "total_participants": 0,
            "total_surveys": 0,
            "total_completed": 0,
            "completion_rate": 0,
            "group_abc": {"chart_data": {"labels": [], "series": []}, "table_data": []},
            "group_d": {"chart_data": {"labels": [], "series": []}, "table_data": []},
        }

    grade_filter = request.GET.get("grade")
    gender_filter = request.GET.get("gender")

    # --- 1. Constants ID chuẩn theo cấu trúc DB ---
    GENDER_QUESTION_ID = 23
    GRADE_QUESTION_ID = 22

    MALE_ID = 110
    FEMALE_ID = 111
    OTHER_IDS = [112, 113]  # Gộp Khác (112) và Không muốn trả lời (113)

    GENDER_MAP = {
        "male": 110,
        "female": 111,
        "other": [112, 113],
        "no_answer": [112, 113],
    }

    GRADE_MAP = {
        "6": [106],
        "7": [107],
        "8": [108],
        "9": [109],
        "10": [156],
        "11": [157],
        "12": [158],
        "thcs": [106, 107, 108, 109],
        "thpt": [156, 157, 158],
    }

    # 2. Lọc SurveySubmission theo khảo sát và thời gian
    survey_submissions = SurveySubmission.objects.filter(survey=survey).select_related('submission')
    survey_submissions = _get_time_filtered_queryset(survey_submissions, request, date_field="submission__submitted_at")

    # 3. Lọc nâng cao theo Giới tính & Cấp/Khối bằng ID
    if gender_filter in GENDER_MAP:
        opt_val = GENDER_MAP[gender_filter]
        if isinstance(opt_val, list):
            survey_submissions = survey_submissions.filter(
                submission__answers__question_id=GENDER_QUESTION_ID,
                submission__answers__selected_option_id__in=opt_val
            )
        else:
            survey_submissions = survey_submissions.filter(
                submission__answers__question_id=GENDER_QUESTION_ID,
                submission__answers__selected_option_id=opt_val
            )

    if grade_filter in GRADE_MAP:
        opt_ids = GRADE_MAP[grade_filter]
        survey_submissions = survey_submissions.filter(
            submission__answers__question_id=GRADE_QUESTION_ID,
            submission__answers__selected_option_id__in=opt_ids
        )

    survey_submissions = survey_submissions.distinct()
    total_surveys = survey_submissions.count()

    # 4. Thống kê số lượng người tham gia độc lập
    submissions_qs = Submission.objects.filter(survey_results__survey=survey).distinct()
    submissions_qs = _get_time_filtered_queryset(submissions_qs, request, date_field="submitted_at")

    if gender_filter in GENDER_MAP:
        opt_val = GENDER_MAP[gender_filter]
        if isinstance(opt_val, list):
            submissions_qs = submissions_qs.filter(
                answers__question_id=GENDER_QUESTION_ID,
                answers__selected_option_id__in=opt_val
            )
        else:
            submissions_qs = submissions_qs.filter(
                answers__question_id=GENDER_QUESTION_ID,
                answers__selected_option_id=opt_val
            )

    if grade_filter in GRADE_MAP:
        submissions_qs = submissions_qs.filter(
            answers__question_id=GRADE_QUESTION_ID,
            answers__selected_option_id__in=GRADE_MAP[grade_filter]
        )

    submissions_qs = submissions_qs.distinct()

    users_count = submissions_qs.filter(user__isnull=False).values("user").distinct().count()
    
    has_lead = hasattr(Submission, 'lead')
    leads_count = submissions_qs.filter(user__isnull=True, lead__isnull=False).values("lead").distinct().count() if has_lead else 0
    
    guests_filter = Q(user__isnull=True, session_key__isnull=False)
    if has_lead:
        guests_filter &= Q(lead__isnull=True)
    sessions_count = submissions_qs.filter(guests_filter).values("session_key").distinct().count()

    total_participants = users_count + leads_count + sessions_count

    # 5. Xử lý thống kê nhóm ABC
    thresholds_abc = SurveyResultThreshold.objects.filter(
        survey=survey, group__code__iexact='all_abc'
    ).order_by("min_score")

    labels_abc, series_abc, table_abc = [], [], []
    for t in thresholds_abc:
        sub_abc = survey_submissions.annotate(
            score_abc=F('submission__score_a') + F('submission__score_b') + F('submission__score_c')
        ).filter(
            score_abc__gte=t.min_score, score_abc__lte=t.max_score
        )

        count = sub_abc.count()

        # Thống kê Nam (110)
        count_male = sub_abc.filter(
            submission__answers__question_id=GENDER_QUESTION_ID,
            submission__answers__selected_option_id=MALE_ID
        ).count()

        # Thống kê Nữ (111)
        count_female = sub_abc.filter(
            submission__answers__question_id=GENDER_QUESTION_ID,
            submission__answers__selected_option_id=FEMALE_ID
        ).count()

        # Thống kê Khác / Không trả lời (112 + 113)
        count_other = sub_abc.filter(
            submission__answers__question_id=GENDER_QUESTION_ID,
            submission__answers__selected_option_id__in=OTHER_IDS
        ).count()

        pct = f"{round((count / total_surveys) * 100, 1)}%" if total_surveys > 0 else "0.0%"
        pct_male = f"{round((count_male / count) * 100, 1)}%" if count > 0 else "0.0%"
        pct_female = f"{round((count_female / count) * 100, 1)}%" if count > 0 else "0.0%"
        pct_other = f"{round((count_other / count) * 100, 1)}%" if count > 0 else "0.0%"

        labels_abc.append(t.title)
        series_abc.append(count)
        table_abc.append({
            "title": t.title,
            "score_range": f"{t.min_score} – {t.max_score} điểm",
            "count": count,
            "percentage": pct,
            "male_count": count_male,
            "male_percentage": pct_male,
            "female_count": count_female,
            "female_percentage": pct_female,
            "other_count": count_other,
            "other_percentage": pct_other,
            "description": getattr(t, 'description', ''),
            "recommendation": getattr(t, 'recommendation', ''),
        })

    # 6. Xử lý thống kê nhóm D
    thresholds_d = SurveyResultThreshold.objects.filter(
        survey=survey, group__code__iexact='D'
    ).order_by("min_score")

    labels_d, series_d, table_d = [], [], []
    for t in thresholds_d:
        sub_d = survey_submissions.filter(
            submission__score_d__gte=t.min_score,
            submission__score_d__lte=t.max_score
        )

        count = sub_d.count()

        # Thống kê Nam (110)
        count_male = sub_d.filter(
            submission__answers__question_id=GENDER_QUESTION_ID,
            submission__answers__selected_option_id=MALE_ID
        ).count()

        # Thống kê Nữ (111)
        count_female = sub_d.filter(
            submission__answers__question_id=GENDER_QUESTION_ID,
            submission__answers__selected_option_id=FEMALE_ID
        ).count()

        # Thống kê Khác / Không trả lời (112 + 113)
        count_other = sub_d.filter(
            submission__answers__question_id=GENDER_QUESTION_ID,
            submission__answers__selected_option_id__in=OTHER_IDS
        ).count()

        pct = f"{round((count / total_surveys) * 100, 1)}%" if total_surveys > 0 else "0.0%"
        pct_male = f"{round((count_male / count) * 100, 1)}%" if count > 0 else "0.0%"
        pct_female = f"{round((count_female / count) * 100, 1)}%" if count > 0 else "0.0%"
        pct_other = f"{round((count_other / count) * 100, 1)}%" if count > 0 else "0.0%"

        labels_d.append(t.title)
        series_d.append(count)
        table_d.append({
            "title": t.title,
            "score_range": f"{t.min_score} – {t.max_score} điểm",
            "count": count,
            "percentage": pct,
            "male_count": count_male,
            "male_percentage": pct_male,
            "female_count": count_female,
            "female_percentage": pct_female,
            "other_count": count_other,
            "other_percentage": pct_other,
            "description": getattr(t, 'description', ''),
            "recommendation": getattr(t, 'recommendation', ''),
        })

    return {
        "status": "success",
        "total_participants": total_participants,
        "total_surveys": total_surveys,
        "total_completed": total_surveys,
        "completion_rate": 100 if total_surveys > 0 else 0,
        "group_abc": {
            "chart_data": {"labels": labels_abc, "series": series_abc},
            "table_data": table_abc,
        },
        "group_d": {
            "chart_data": {"labels": labels_d, "series": series_d},
            "table_data": table_d,
        },
    }



# Impot thêm các models của bạn (User, Survey, Submission, SurveySubmission, v.v.)

def reconnect360_repeat_users_view(
    request,
    slug="khao-sat-xu-huong-thu-minh-va-muc-do-ket-noi-xa-hoi-o-hoc-sinh-thcs-thpt-1",
):
    """View hiển thị trang thống kê học sinh làm bài khảo sát từ 2 lần trở lên."""
    survey = Survey.objects.filter(slug=slug, is_active=True).first()
    if not survey:
        survey = Survey.objects.filter(is_active=True).first()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        data = _get_repeat_users_report_data(survey, request)
        return JsonResponse(data)

    return render(
        request, 
        "surveys/reconnect360/repeat_users.html", 
        {"survey": survey}
    )

def _get_repeat_users_report_data(survey, request):
    if not survey:
        return {"status": "error", "message": "Không tìm thấy khảo sát!", "users": [], "total_repeat_users": 0}

    grade_filter = request.GET.get("grade")
    gender_filter = request.GET.get("gender")

    GENDER_QUESTION_ID = 23
    GRADE_QUESTION_ID = 22

    GENDER_MAP = {"male": 110, "female": 111, "other": [112, 113], "no_answer": [112, 113]}
    GRADE_MAP = {
        "6": [106], "7": [107], "8": [108], "9": [109],
        "10": [156], "11": [157], "12": [158],
        "thcs": [106, 107, 108, 109], "thpt": [156, 157, 158]
    }

    submissions_qs = Submission.objects.filter(survey_results__survey=survey).distinct()
    submissions_qs = _get_time_filtered_queryset(submissions_qs, request, date_field="submitted_at")

    if gender_filter in GENDER_MAP:
        opt_val = GENDER_MAP[gender_filter]
        if isinstance(opt_val, list):
            submissions_qs = submissions_qs.filter(answers__question_id=GENDER_QUESTION_ID, answers__selected_option_id__in=opt_val)
        else:
            submissions_qs = submissions_qs.filter(answers__question_id=GENDER_QUESTION_ID, answers__selected_option_id=opt_val)

    if grade_filter in GRADE_MAP:
        submissions_qs = submissions_qs.filter(answers__question_id=GRADE_QUESTION_ID, answers__selected_option_id__in=GRADE_MAP[grade_filter])

    submissions_qs = submissions_qs.distinct()

    # Tìm User làm bài từ 2 lần trở lên
    user_counts = (
        submissions_qs.filter(user__isnull=False)
        .values("user")
        .annotate(total_attempts=Count("id"))
        .filter(total_attempts__gte=2)
    )

    repeat_user_ids = [item["user"] for item in user_counts]
    report_list = []

    from django.contrib.auth import get_user_model
    User = get_user_model()
    users = User.objects.filter(id__in=repeat_user_ids)

    for user_obj in users:
        user_submissions = submissions_qs.filter(user=user_obj).order_by("submitted_at")
        first_sub = user_submissions.first()
        latest_sub = user_submissions.last()

        if not first_sub or not latest_sub:
            continue

        # --- KIỂM TRA ĐIỀU KIỆN: Lần 1 và Lần 2 cách nhau hơn 7 ngày ---
        time_difference = latest_sub.submitted_at - first_sub.submitted_at
       # if time_difference <= timedelta(days=7):
           # continue  # Bỏ qua nếu cách nhau <= 7 ngày

        first_abc = (first_sub.score_a or 0) + (first_sub.score_b or 0) + (first_sub.score_c or 0)
        latest_abc = (latest_sub.score_a or 0) + (latest_sub.score_b or 0) + (latest_sub.score_c or 0)
        diff_abc = latest_abc - first_abc

        first_d = first_sub.score_d or 0
        latest_d = latest_sub.score_d or 0
        diff_d = latest_d - first_d

        gender_ans = latest_sub.answers.filter(question_id=GENDER_QUESTION_ID).first()
        gender_text = gender_ans.selected_option.text if gender_ans and gender_ans.selected_option else "—"

        grade_ans = latest_sub.answers.filter(question_id=GRADE_QUESTION_ID).first()
        grade_text = grade_ans.selected_option.text if grade_ans and grade_ans.selected_option else "—"

        user_display_name = (
            f"{user_obj.last_name} {user_obj.first_name}".strip()
            or getattr(user_obj, 'full_name', '')
            or user_obj.username
        )

        report_list.append({
            "user_id": user_obj.id,
            "full_name": user_display_name,
            "email": user_obj.email or "—",
            "gender": gender_text,
            "grade": grade_text,
            "first_date": first_sub.submitted_at.strftime("%d/%m/%Y %H:%M"),
            "latest_date": latest_sub.submitted_at.strftime("%d/%m/%Y %H:%M"),
            "days_diff": time_difference.days,  # Số ngày chênh lệch giữa 2 lần
            "first_abc": first_abc,
            "latest_abc": latest_abc,
            "diff_abc": diff_abc,
            "first_d": first_d,
            "latest_d": latest_d,
            "diff_d": diff_d,
            "first_scores": {
                "abc": first_abc,
                "d": first_d,
            },
            "latest_scores": {
                "abc": latest_abc,
                "d": latest_d,
            }
        })

    # Hàm phụ tách số lớp từ chuỗi (ví dụ: "Lớp 6" -> 6)
    def extract_grade_number(grade_str):
        import re
        match = re.search(r'\d+', grade_str)
        return int(match.group()) if match else 0

    # Sắp xếp theo thứ tự: Khối lớp tăng dần (6 -> 12), nếu cùng khối thì sắp xếp theo họ tên
    report_list.sort(key=lambda x: (extract_grade_number(x["grade"]), x["full_name"]))

    return {
        "status": "success",
        "total_repeat_users": len(report_list),
        "users": report_list,
    }
#
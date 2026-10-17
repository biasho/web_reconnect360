from collections import Counter
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.db.models import Q

from .forms import RegisterForm
from .models import UserProfile
from surveys.models import Submission


# 1. Xử lý Đăng ký Tài khoản
def register_view(request):
    if request.user.is_authenticated:
        return redirect('pages:home')
        
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, "Đăng ký tài khoản thành công!")
            return redirect('pages:home')
    else:
        form = RegisterForm()
        
    return render(request, 'users/register.html', {'form': form})


# 2. Xử lý Đăng nhập
def login_view(request):
    if request.user.is_authenticated:
        return redirect('pages:home')

    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            
            next_url = request.GET.get('next')
            if next_url:
                return redirect(next_url)
            return redirect('pages:home')
        else:
            messages.error(request, "Tên đăng nhập hoặc mật khẩu không chính xác.")
    else:
        form = AuthenticationForm()
        
    return render(request, 'users/login.html', {'form': form})


# 3. Xử lý Đăng xuất
def logout_view(request):
    logout(request)
    messages.info(request, "Bạn đã đăng xuất khỏi hệ thống.")
    return redirect('users:login')


# 4. Trang Hồ sơ cá nhân
@login_required(login_url='users:login')
def profile_view(request):
    profile, created = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        request.user.first_name = first_name
        request.user.last_name = last_name
        request.user.save()

        profile.phone_number = request.POST.get('phone_number', '').strip()
        profile.bio = request.POST.get('bio', '').strip()

        if 'avatar' in request.FILES:
            if profile.avatar and profile.avatar.name != 'avatars/default.png':
                profile.avatar.delete(save=False)
            profile.avatar = request.FILES['avatar']

        profile.save()
        messages.success(request, "Cập nhật thông tin cá nhân thành công!")
        return redirect('users:profile')

    return render(request, 'users/profile.html', {
        'user': request.user,
        'profile': profile
    })


# 5. Trang Cài đặt hệ thống
@login_required(login_url='users:login')
def settings_view(request):
    return render(request, 'users/settings.html')


# 6. Trang Tổng quan Dashboard
@login_required(login_url='users:login')
def dashboard(request):
    user_submissions = (
        Submission.objects.filter(user=request.user)
        .select_related('survey', 'composite_survey')
        .prefetch_related('survey_results', 'survey_results__survey')
        .order_by('-submitted_at')
    )

    total_surveys = user_submissions.count()
    completed_surveys = user_submissions.filter(status='completed').count() if hasattr(Submission, 'status') else total_surveys
    pending_surveys = total_surveys - completed_surveys

    per_page = request.GET.get('per_page', '10')
    try:
        per_page = int(per_page)
    except ValueError:
        per_page = 10

    paginator = Paginator(user_submissions, per_page)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'total_surveys': total_surveys,
        'completed_surveys': completed_surveys,
        'pending_surveys': pending_surveys,
        'page_obj': page_obj,
        'per_page': per_page,
    }

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        html = render_to_string('users/partials/_dashboard_submissions.html', context, request=request)
        return JsonResponse({'html': html})

    return render(request, 'users/dashboard.html', context)


# 7. Chi tiết Lượt nộp & Thống kê Chi tiết (Modal)
@login_required(login_url='users:login')
def submission_detail(request, pk):
    try:
        # Phân quyền
        if request.user.is_staff or request.user.is_superuser:
            query = Q(pk=pk)
        else:
            query = Q(pk=pk) & (Q(user=request.user) | Q(lead__user=request.user))

        submission = get_object_or_404(
            Submission.objects.select_related('survey', 'composite_survey')
                              .prefetch_related(
                                  'survey_results__survey',
                                  'composite_survey__items__survey',
                                  'answers__question',
                                  'answers__selected_option'
                              ),
            query
        )
        
        surveys_data = []

        # 1. Trường hợp BỘ KHẢO SÁT (Composite)
        if submission.composite_survey:
            type_label = "Bộ khảo sát"
            title = submission.composite_survey.title
            
            items_map = {
                item.survey_id: (item.name or item.survey.title)
                for item in submission.composite_survey.items.all()
            }

            results = submission.survey_results.all()
            if results.exists():
                for res in results:
                    survey_title = items_map.get(res.survey_id, res.survey.title if res.survey else "Khảo sát thành phần")
                    surveys_data.append({
                        'survey_title': survey_title,
                        'score': res.score if res.score is not None else 0,
                    })
            else:
                surveys_data.append({
                    'survey_title': title,
                    'score': submission.total_score or 0,
                })

        # 2. Trường hợp KHẢO SÁT ĐƠN (Single)
        elif submission.survey:
            type_label = "Khảo sát đơn"
            title = submission.survey.title
            surveys_data.append({
                'survey_title': title,
                'score': submission.total_score or 0,
            })
            
        else:
            type_label = "Khảo sát"
            title = "Không xác định"
            surveys_data.append({
                'survey_title': title,
                'score': submission.total_score or 0,
            })

        # 3. Thống kê chi tiết từng câu hỏi & điểm số tương ứng của lựa chọn
        stats_data = []
        questions_map = {}

        for ans in submission.answers.all():
            q_id = ans.question_id
            if q_id not in questions_map:
                questions_map[q_id] = {
                    'question_text': ans.question.text,
                    'choices': [],
                    'texts': [],
                }

            # Đáp án trắc nghiệm: Lấy cặp (Tên lựa chọn, Điểm số)
            if ans.selected_option:
                score = getattr(ans.selected_option, 'score', 0)
                questions_map[q_id]['choices'].append((ans.selected_option.text, score))
            
            # Đáp án tự luận
            if ans.text_answer and ans.text_answer.strip():
                questions_map[q_id]['texts'].append(ans.text_answer.strip())

        for q_id, q_data in questions_map.items():
            stats_data.append({
                'question_text': q_data['question_text'],
                'sorted_choices': q_data['choices'] if q_data['choices'] else None,
                'text_answers': q_data['texts'] if q_data['texts'] else None,
            })

        return JsonResponse({
            'id': submission.id,
            'title': title,
            'type_label': type_label,
            'submitted_at': submission.submitted_at.strftime('%d/%m/%Y %H:%M') if submission.submitted_at else '',
            'total_score': submission.total_score or 0,
            'surveys': surveys_data,
            'stats_data': stats_data,
        })

    except Exception as e:
        print(f"❌ LỖI SUBMISSION_DETAIL: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)


#